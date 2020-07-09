from nerf.modules import *


class FlexibleNeRFModel(torch.nn.Module):
    def __init__(
        self,
        num_layers=4,
        hidden_size=128,
        skip_connect_every=4,
        num_encoding_fn_xyz=6,
        num_encoding_fn_dir=4,
        include_input_xyz=True,
        include_input_dir=True,
        log_sampling_xyz=True,
        log_sampling_dir=True,
        use_viewdirs=True,
    ):
        super(FlexibleNeRFModel, self).__init__()
        self.encode_xyz = PositionalEncoding(
            num_encoding_fn_xyz, include_input_xyz, log_sampling_xyz
        )
        self.encode_dir = PositionalEncoding(
            num_encoding_fn_dir, include_input_dir, log_sampling_dir
        )

        include_input_xyz = 3 if include_input_xyz else 0
        include_input_dir = 3 if include_input_dir else 0
        self.dim_xyz = include_input_xyz + 2 * 3 * num_encoding_fn_xyz
        self.dim_dir = include_input_dir + 2 * 3 * num_encoding_fn_dir
        self.skip_connect_every = skip_connect_every
        self.num_layers = num_layers
        if not use_viewdirs:
            self.dim_dir = 0

        self.layer1 = torch.nn.Linear(self.dim_xyz, hidden_size)
        self.layers_xyz = torch.nn.ModuleList()
        for i in range(num_layers - 1):
            if i % self.skip_connect_every == 0 and i > 0 and i != num_layers - 1:
                self.layers_xyz.append(
                    torch.nn.Linear(self.dim_xyz + hidden_size, hidden_size)
                )
            else:
                self.layers_xyz.append(torch.nn.Linear(hidden_size, hidden_size))

        self.use_viewdirs = use_viewdirs
        if self.use_viewdirs:
            self.layers_dir = torch.nn.ModuleList()
            # This deviates from the original paper, and follows the code release instead.
            self.layers_dir.append(
                torch.nn.Linear(self.dim_dir + hidden_size, hidden_size // 2)
            )

            self.fc_alpha = torch.nn.Linear(hidden_size, 1)
            self.fc_rgb = torch.nn.Linear(hidden_size // 2, 3)
            self.fc_feat = torch.nn.Linear(hidden_size, hidden_size)
        else:
            self.fc_out = torch.nn.Linear(hidden_size, 4)

        self.relu = torch.nn.functional.relu

    def forward(self, ray_points, ray_directions=None):
        xyz = self.encode_xyz(ray_points)
        x = self.layer1(xyz)
        for i, layer in enumerate(self.layers_xyz):
            if (
                i % self.skip_connect_every == 0
                and i > 0 and i != self.num_layers - 1
            ):
                x = torch.cat((x, xyz), dim=-1)
            x = self.relu(layer(x))
        if self.use_viewdirs:
            view = self.encode_dir(ray_directions)
            feat = self.relu(self.fc_feat(x))
            alpha = self.fc_alpha(x)
            x = torch.cat((feat, view), dim=-1)
            for l in self.layers_dir:
                x = self.relu(l(x))
            rgb = self.fc_rgb(x)
            return torch.cat((rgb, alpha), dim=-1)
        else:
            return self.fc_out(x)


class SimpleModel(torch.nn.Module):
    def __init__(
        self,
        hidden_layers=4,
        hidden_layers_view=2,
        hidden_size=128,
        num_encoding_fn_xyz=128,
        num_encoding_fn_dir=4,
        include_input_xyz=True,
        include_input_dir=True,
        log_sampling_xyz=True,
        log_sampling_dir=True,
        skip_count=1,

        **kwargs
    ):
        super(SimpleModel, self).__init__()
        self.encode_xyz = SpatialEmbedding(3,num_encoding_fn_xyz,8)
        self.encode_dir = PositionalEncoding(
            num_encoding_fn_dir, include_input_dir, log_sampling_dir
        )
        self.layer0 = SimpleModule(self.encode_xyz.output_size(), hidden_size)
        self.hidden_all = MultiSkipModule(hidden_size, self.encode_xyz.output_size(),
                                          hidden_layers, skip_count=skip_count)
        self.color = SimpleModule(hidden_size, 3, activation=torch.nn.Sigmoid())
        self.depth = torch.nn.Linear(hidden_size, 1)

        self.hidden_layers_view_amount = hidden_layers_view
        if hidden_layers_view >= 0:
            self.hidden_view = MultiSkipModule(hidden_size,
                                              self.encode_xyz.output_size()+self.encode_dir.output_size(),
                                              hidden_layers_view)

    def forward(self, ray_points, ray_directions=None):
        xyz = self.encode_xyz(ray_points)
        x = self.layer0(xyz)
        x = self.hidden_all(x, xyz)
        depth = self.depth(x)
        if self.hidden_layers_view_amount >= 0 and ray_directions is not None:
            xyzdir = torch.cat((xyz, self.encode_dir(ray_directions)), dim=-1)
            x = self.hidden_view(x,xyzdir)
        color = self.color(x)
        return torch.cat([color,depth], dim=-1)


class SpecularSimpleModel(torch.nn.Module):
    def __init__(
        self,
        hidden_layers=4,
        hidden_layers_view=2,
        hidden_size=128,
        num_encoding_fn_xyz=128,
        num_encoding_fn_dir=4,
        include_input_xyz=True,
        include_input_dir=True,
        log_sampling_xyz=True,
        log_sampling_dir=True,
        skip_count=1,
        luminance_function="min1",
        **kwargs
    ):
        super(SpecularSimpleModel, self).__init__()
        self.encode_xyz = SpatialEmbedding(3,num_encoding_fn_xyz,8)
        self.encode_dir = PositionalEncoding(
            num_encoding_fn_dir, include_input_dir, log_sampling_dir
        )
        self.layer0 = SimpleModule(self.encode_xyz.output_size(), hidden_size)
        self.hidden_all = MultiSkipModule(hidden_size, self.encode_xyz.output_size(),
                                          hidden_layers, skip_count=skip_count)
        self.color = SimpleModule(hidden_size, 3, activation=torch.nn.Sigmoid())
        self.depth = torch.nn.Linear(hidden_size, 1)

        self.hidden_layers_view_amount = hidden_layers_view
        if hidden_layers_view >= 0:
            self.hidden_view = MultiSkipModule(hidden_size,
                                              self.encode_xyz.output_size()+self.encode_dir.output_size(),
                                              hidden_layers_view)
            self.specular = SimpleModule(hidden_size, 1, activation=torch.nn.Tanh())
            self.combine = get_luminance_function(luminance_function)

    def forward(self, ray_points, ray_directions=None):
        xyz = self.encode_xyz(ray_points)
        x = self.layer0(xyz)
        x = self.hidden_all(x, xyz)
        depth = self.depth(x)
        color = self.color(x)
        if self.hidden_layers_view_amount >= 0 and ray_directions is not None:
            xyzdir = torch.cat((xyz, self.encode_dir(ray_directions)), dim=-1)
            x = self.hidden_view(x,xyzdir)
            specular = torch.nn.functional.relu(self.specular(x))
            color = self.combine(color, specular)
        return torch.cat([color,depth], dim=-1), specular


class FlatModel(torch.nn.Module):
    def __init__(
        self,
        hidden_size=256,
        **kwargs
    ):
        super(FlatModel, self).__init__()
        self.embed = SimpleSpatialEmbedding(3, hidden_size, 10)
        self.model0 = SimpleModule(2*hidden_size, 2*hidden_size)
        self.model1 = SimpleModule(4*hidden_size, hidden_size)

        self.depth = SimpleModule(hidden_size, 1)
        self.color = SimpleModule(hidden_size, 3, activation=torch.nn.Sigmoid())


    def forward(self, ray_points, ray_directions=None):
        x = self.embed(ray_points)
        x_hat = self.model0(x)
        x = self.model1(torch.cat([x,x_hat], dim=-1))
        depth = self.depth(x)
        color = self.color(x)
        return torch.cat([color,depth], dim=-1)


class ResModel(torch.nn.Module):
    def __init__(
        self,
        hidden_size=128,
        hidden_layers=2,
        num_encoding_fn_xyz=128,
        **kwargs
    ):
        super(ResModel, self).__init__()
        self.embed = SimpleSpatialEmbedding(3, num_encoding_fn_xyz, 8)
        self.model0 = SimpleModule(self.embed.output_size(), hidden_size)
        self.model1 = torch.nn.Sequential(
            *[ResBlock(hidden_size, hidden_size//2) for _ in range(hidden_layers)]
        )

        self.depth = SimpleModule(hidden_size, 1)
        self.color = SimpleModule(hidden_size, 3, activation=torch.nn.Sigmoid())


    def forward(self, ray_points, ray_directions=None):
        x = self.embed(ray_points)
        x_hat = self.model0(x)
        x = self.model1(x_hat)
        depth = self.depth(x)
        color = self.color(x)
        return torch.cat([color,depth], dim=-1)

