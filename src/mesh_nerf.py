import argparse
import os
import time
import numpy as np
import torch
import torchvision
import yaml

from pathlib import Path
from skimage import measure
from scipy.spatial import KDTree
from tqdm import tqdm
try:
    import marching_cubes as mcubes
except:
    print("Error, justus' version of pymcubes not installed!")
    print("""
Run the following commands(Note that its currently not possible to install through a package manager, since it depends on eigen):

poetry shell
cd ../additional_dependencies/PyMarchingCubes
python setup.py install
""")
from nerf import (
    CfgNode,
    models,
    get_embedding_function,
    predict_and_render_radiance,
)
from train_nerf import NeRFModel, nest_dict


def export_obj(vertices, triangles, diffuse, normals, filename):
    """
    Exports a mesh in the (.obj) format.
    """

    with open(filename, "w") as fh:

        for index, v in enumerate(vertices):
            fh.write("v {} {} {} {} {} {}\n".format(*v, *diffuse[index]))

        for n in normals:
            fh.write("vn {} {} {}\n".format(*n))

        for f in triangles:
            fh.write("f")
            for index in f:
                fh.write(" {}//{}".format(index + 1, index + 1))

            fh.write("\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "folder", type=str, help="Path to Log Folder"
    )
    parser.add_argument(
        "--limit",
        type=float,
        help="Limits in -xyz to xyz for mcubes.",
        default=1.0
    )
    parser.add_argument(
        "--res",
        type=int,
        help="Sampling resolution for mcubes.",
        default=128
    )
    parser.add_argument(
        "--save-dir",
        type=str,
        help="Save mesh to this directory, if specified.",
        default=".",
    )
    parser.add_argument(
        "--super-sampling",
        type=int,
        help="Add super sampling along the edges.",
        default=0,
    )

    parser.add_argument("--cache-mesh", dest="cache_mesh", action="store_true")
    parser.add_argument("--no-cache-mesh", dest="cache_mesh", action="store_false")
    parser.set_defaults(cache_mesh=True)


    args = parser.parse_args()
    log_folder = Path(args.folder)
    with (log_folder / "hparams.yaml").open() as f:
        hparams = yaml.load(f, Loader=yaml.FullLoader)
        cfg = CfgNode(nest_dict(hparams, sep="."))

    if torch.cuda.is_available():
        device = torch.cuda.current_device()
    else:
        device = "cpu"
    try:
        checkpoint_path = next(log_folder.glob('**/*.ckpt'))
    except:
        raise FileNotFoundError("Could not find a .ckpt file in folder ", args.checkpoint)
    model = NeRFModel.load_from_checkpoint(checkpoint_path, cfg=cfg)
    model.eval()
    model.to(device)

    # Mesh Extraction
    N = args.res
    iso_value = 17
    batch_size = 160*8*8
    density_samples_count = 6
    chunk = int(density_samples_count / 2)
    distance_length = 0.001
    distance_threshold = 0.001
    limit = args.limit
    view_disparity = 2 * limit / N
    t = np.linspace(-limit, limit, N)
    sampling_method = 0
    adjust_normals = False
    specific_view = False
    with torch.no_grad():
        vertices, triangles, normals, diffuse = None, None, None, None
        if args.cache_mesh:
            print("Generating mesh geometry...")
            if args.super_sampling >= 1:
                grid_alpha_x, pts_flat_x = sample_points((N+(N-1)*args.super_sampling, N, N), batch_size, device, model, limit)
                grid_alpha_y, pts_flat_y = sample_points((N, N+(N-1)*args.super_sampling, N), batch_size, device, model, limit)
                grid_alpha_z, pts_flat_z = sample_points((N, N, N+(N-1)*args.super_sampling), batch_size, device, model, limit)
                if iso_value is None:
                    iso_value_x = np.maximum(grid_alpha_x, 0).mean()
                    iso_value_y = np.maximum(grid_alpha_x, 0).mean()
                    iso_value_z = np.maximum(grid_alpha_x, 0).mean()
                    iso_value = np.mean([iso_value_x, iso_value_y, iso_value_z])
                print("Iso-Value:", iso_value)
                vertices, triangles = mcubes.marching_cubes_super_sampling(grid_alpha_x, grid_alpha_y, grid_alpha_z, iso_value)
                vertices = np.ascontiguousarray(vertices)
                mcubes.export_obj(vertices,triangles, os.path.join(args.save_dir, "mesh.obj"))
                return
            else:

                grid_alpha, pts_flat = sample_points(N, batch_size, device, model, limit)
                # Dynamic iso-value, should be slightly over zero
                if iso_value is None:
                    iso_value = np.maximum(grid_alpha, 0).mean()
                print("Iso-Value:", iso_value)

                # Extracting iso-surface triangulated
                vertices, triangles, normals, values = measure.marching_cubes(
                    grid_alpha, iso_value
                )
                vertices = np.ascontiguousarray(vertices)
                normals = np.ascontiguousarray(normals)

            if adjust_normals:
                adjusting_normals(N, chunk, density_samples_count, distance_length,
                                  limit, normals, pts_flat, sampling_method, vertices)

            np.save(
                os.path.join(args.save_dir, args.save_dir+"mesh_cache.npy"),
                (vertices, triangles, normals),
            )
            print("Mesh geometry saved successfully")
        else:
            print("Loading mesh geometry...")
            vertices, triangles, normals = np.load(
                os.path.join(args.save_dir, "mesh_cache.npy"), allow_pickle=True
            )

        print("Generating mesh texture...")

        #targets = torch.from_numpy(vertices) / N * 2 * limit - limit
        targets = torch.from_numpy(vertices)

        inv_normals = -torch.from_numpy(normals)
        # Reorder coordinates
        targets = targets[:, [1, 0, 2]]
        inv_normals = inv_normals[:, [1, 0, 2]]

        # Query directly without specific-views
        if specific_view:
            diffuse = np.zeros((len(targets), 3))
            for idx in tqdm(range(0, len(targets) // batch_size + 1)):
                offset1 = batch_size * idx
                offset2 = np.minimum(batch_size * (idx + 1), len(vertices))
                pos_batch = targets[offset1:offset2].to(device)
                normal_batch = inv_normals[offset1:offset2].to(device)

                result_batch = model.sample_points(pos_batch, normal_batch)
                #result_batch = model.sample_points(pos_batch, None)

                # Current color hack since the network does not normalize colors
                result_batch = torch.nn.functional.sigmoid(result_batch[..., :3]) * 255
                # Query the whole diffuse map
                diffuse[offset1:offset2] = result_batch[..., :3].cpu().detach().numpy()
        if not specific_view:

            # Move ray origins slightly out of the mesh
            # ray_origins = length * directions
            ray_origins = targets - view_disparity * inv_normals

            # Careful, do not set the near plane to zero!
            ray_bounds = (
                torch.tensor([0.001, 2 * view_disparity], dtype=ray_origins.dtype)
                .view(1, 2)
                .expand(batch_size, 2)
            ).to(device)

            pred = []
            for idx in tqdm(range(0, len(targets) // batch_size + 1)):
                offset1 = batch_size * idx
                offset2 = np.minimum(batch_size * (idx + 1), len(vertices))
                pos_batch = ray_origins[offset1:offset2].to(device)
                normal_batch = inv_normals[offset1:offset2].to(device)
                ray_bounds_batch = ray_bounds[: offset2 - offset1]

                _, _, _, diffuse, _, _ = model((pos_batch, normal_batch, ray_bounds_batch))


                pred.append(diffuse.cpu().detach())

            # Query the whole diffuse map
            diffuse = torch.cat(pred, dim=0).numpy()

    # If diffuse should be normalized
    #diff_range = diffuse.max() -diffuse.min()
    #diffuse = (diffuse - diffuse.min()) /diff_range * 255

    # Export model
    print("Saving final model...", end="")
    export_obj(
        vertices,
        triangles,
        diffuse,
        normals,
        os.path.join(args.save_dir, "mesh.obj"),
    )
    print("Saved!")


def adjusting_normals(N, chunk, density_samples_count, distance_length, limit, normals,
                      pts_flat, sampling_method, vertices):
    # Re-adjust normals based on NERF's density grid
    # Create a density KDTree look-up table
    tree = KDTree(pts_flat) if sampling_method == 0 else None
    # Create some density samples
    density_samples = np.linspace(
        -distance_length, distance_length, density_samples_count
    )[:, np.newaxis]
    # Adjust normals with the assumption of having proper geometry
    print("Adjusting normals")
    for index, vertex in enumerate(tqdm(vertices)):
        vertex_norm = vertex[[1, 0, 2]] / N * 2 * limit - limit
        vertex_direction = normals[index][[1, 0, 2]]

        # Sample points across the ray direction (a.k.a normal)
        samples = (
                vertex_norm[np.newaxis, :].repeat(density_samples_count, 0)
                + vertex_direction[np.newaxis, :].repeat(density_samples_count, 0)
                * density_samples
        )

        def extract_cum_density(samples):
            inliers_indices = None
            if sampling_method == 0:
                # Sample 1th nearest neighbor
                distances, indices = tree.query(samples, 1)

                # Filter outliers
                inliers_indices = indices[distances <= distance_threshold]
            elif sampling_method == 1:
                # Sample based on grid proximity
                indices = (
                    (
                            np.around((samples + limit) / 2 / limit * N)
                            * N ** np.arange(2, -1, -1)
                    )
                        .sum(1)
                        .astype(int)
                )

                # Filtering exceeding boundaries
                inliers_indices = indices[~(indices >= N ** 3)]
            else:
                # Sample based on re-computing the radiance field
                indices = (
                    (
                            np.around((samples + limit) / 2 / limit * N)
                            * N ** np.arange(2, -1, -1)
                    )
                        .sum(1)
                        .astype(int)
                )

                # Filtering exceeding boundaries
                inliers_indices = indices[~(indices >= N ** 3)]

            return density[inliers_indices].sum()

        # Extract densities
        sample_density_1 = extract_cum_density(samples[:chunk])
        sample_density_2 = extract_cum_density(samples[chunk:])

        # Re-direct the normal
        if sample_density_1 < sample_density_2:
            normals[index] *= -1


def sample_points(N, batch_size, device, model, limit, color=False):
    if isinstance(N, tuple):
        x,y,z = N
        t_x = np.linspace(-limit, limit, x)
        t_y = np.linspace(-limit, limit, y)
        t_z = np.linspace(-limit, limit, z)
        query_pts = np.stack(np.meshgrid(t_y,t_x,t_z), -1).astype(np.float32)
    else:
        x,y,z = N,N,N
        t = np.linspace(-limit, limit, N)
        query_pts = np.stack(np.meshgrid(t,t,t), -1).astype(np.float32)
    pts = torch.from_numpy(query_pts)
    dimension = pts.shape[-1]
    pts_flat = pts.reshape((-1, dimension))
    pts_flat_batch = pts_flat.reshape((-1, batch_size, dimension))
    density = np.zeros((pts_flat.shape[0]))
    if color:
        colors = np.zeros((pts_flat.shape[0], 3))
    for idx, batch in enumerate(tqdm(pts_flat_batch)):
        batch = batch.to(device)
        result_batch = model.sample_points(
            batch, batch
        )  # Reuse positions as fake rays

        # Extracting the density
        density[idx * batch_size: (idx + 1) * batch_size] = (
            result_batch[..., 3].cpu().detach().numpy()
        )
        if color:
            colors[idx * batch_size: (idx + 1) * batch_size] = (
                result_batch[..., :3].cpu().detach().numpy()
            )
    # Create a 3D density grid

    grid_alpha = density.reshape((x, y, z))
    if color:
        grid_color = colors.reshape((x,y,z,3))
        return grid_alpha, grid_color, pts_flat
    return grid_alpha, pts_flat


if __name__ == "__main__":
    main()
