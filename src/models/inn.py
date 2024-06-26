from typing import Type, Callable, Union, Dict
import math
import torch
import torch.nn as nn
import numpy as np
import FrEIA.framework as ff
import FrEIA.modules as fm

from .spline_blocks import RationalQuadraticSplineBlock
from .layers import VBLinear, MixtureDistribution
from .madnis.models.flow import FlowMapping
#from .madnis.mappings.coupling.splines import RationalQuadraticSplineBlock
from .madnis.mappings.coupling.linear import AffineCoupling


class MLP(nn.Module):
    """
    Creates a dense subnetwork
    which can be used within the invertible modules.
    """

    def __init__(
        self,
        meta: Dict,
        features_in: int,
        features_out: int,
        pass_inputs: bool = False
    ):
        """
        Args:
          meta:
            Dictionary with defining parameters
            to construct the network.
          features_in:
            Number of input features.
          features_out:
            Number of output features.
          pass_inputs:
            If True, a tuple is expected as input to forward and only the first tensor
            is used as input to the network
        """
        super().__init__()

        # which activation
        if isinstance(meta["activation"], str):
            try:
                activation = {
                    "relu": nn.ReLU,
                    "elu": nn.ELU,
                    "leakyrelu": nn.LeakyReLU,
                    "tanh": nn.Tanh
                }[meta["activation"]]
            except KeyError:
                raise ValueError(f'Unknown activation "{meta["activation"]}"')
        else:
            activation = meta["activation"]

        layer_constructor = meta.get("layer_constructor", nn.Linear)
        layer_args = {}
        if layer_constructor == VBLinear:
            layer_args = {}
            layer_args["prior_prec"] = meta.get("prior_prec", 1)
            layer_args["std_init"] = meta.get("std_init", -9)

        # Define the layers
        input_dim = features_in
        layers = []
        for i in range(meta["layers"] - 1):
            layers.append(layer_constructor(
                input_dim,
                meta["units"]
            ))
            layers.append(activation())
            input_dim = meta["units"]
        layers.append(layer_constructor(
            input_dim,
            features_out,
            **layer_args
        ))
        nn.init.zeros_(layers[-1].weight)
        nn.init.zeros_(layers[-1].bias)
        self.layers = nn.Sequential(*layers)
        self.pass_inputs = pass_inputs

    def forward(self, x):
        if self.pass_inputs:
            x, *rest = x
            return self.layers(x), *rest
        else:
            return self.layers(x)


class Subnet(nn.Module):
    """
    Standard MLP or bayesian network to be used as a trainable subnet in INNs
    """

    def __init__(
        self,
        num_layers: int,
        size_in: int,
        size_out: int,
        internal_size: int,
        dropout: float = 0.0,
        layer_class: Type = nn.Linear,
        layer_args: dict = {},
        bayesian_last=False
    ):
        """
        Constructs the subnet.

        Args:
            num_layers: number of layers
            size_in: input size of the subnet
            size: output size of the subnet
            internal_size: hidden size of the subnet
            dropout: dropout chance of the subnet
            layer_class: class to construct the linear layers
            layer_args: keyword arguments to pass to the linear layer
        """
        super().__init__()
        if num_layers < 1:
            raise (ValueError("Subnet size has to be 1 or greater"))
        self.layer_list = []
        if not bayesian_last:
            for n in range(num_layers):
                input_dim, output_dim = internal_size, internal_size
                if n == 0:
                    input_dim = size_in
                if n == num_layers - 1:
                    output_dim = size_out
                self.layer_list.append(layer_class(input_dim, output_dim, **layer_args))

                if n < num_layers - 1:
                    if dropout > 0:
                        self.layer_list.append(nn.Dropout(p=dropout))
                    self.layer_list.append(nn.ReLU())
        else:
            for n in range(num_layers):
                input_dim, output_dim = internal_size, internal_size
                if n == 0:
                    input_dim = size_in
                if n == num_layers - 1:
                    output_dim = size_out
                    self.layer_list.append(VBLinear(input_dim, output_dim))
                else:
                    self.layer_list.append(nn.Linear(input_dim, output_dim))
                if n < num_layers - 1:
                    if dropout > 0:
                        self.layer_list.append(nn.Dropout(p=dropout))
                    self.layer_list.append(nn.ReLU())

        self.layers = nn.Sequential(*self.layer_list)

        for name, param in self.layer_list[-1].named_parameters():
            if "logsig2_w" not in name:
                param.data *= 0.02

    def forward(self, x):
        return self.layers(x)


class INN(nn.Module):
    """
    Class implementing a standard conditional INN
    """

    def __init__(self, params: dict):
        """
        Initializes and builds the conditional INN

        Args:
            dims_in: dimension of input
            dims_c: dimension of condition
            params: dictionary with architecture/hyperparameters
        """
        super().__init__()
        self.params = params
        self.dims_in = params["dims_in"]
        self.dims_c = params["dims_c"]
        self.bayesian = params.get("bayesian", False)
        self.bayesian_transfer = False
        if self.bayesian:
            self.bayesian_samples = params.get("bayesian_samples", 20)
            self.bayesian_layers = []
            self.bayesian_factor = params.get("bayesian_factor", 1)

        self.latent_space = self.params.get("latent_space", "gaussian")
        if self.latent_space == "gaussian":
            self.latent_dist = torch.distributions.multivariate_normal.MultivariateNormal(
                torch.zeros(self.dims_in), torch.eye(self.dims_in))
            print(f"        latent space: gaussian")
        elif self.latent_space == "uniform":
            uniform_bounds = self.params.get("uniform_bounds", [0., 1.])
            self.uniform_logprob = uniform_bounds[1]-uniform_bounds[0]
            self.latent_dist = torch.distributions.uniform.Uniform(
                torch.full((self.dims_in,), uniform_bounds[0]), torch.full((self.dims_in,), uniform_bounds[1]))
            print(f"        latent space: uniform with bounds {uniform_bounds}")
        elif self.latent_space == "mixture":
            self.uniform_channels = self.params.get("uniform_channels")
            self.normal_channels = [i for i in range(self.dims_in) if i not in self.uniform_channels]
            self.latent_dist = MixtureDistribution(normal_channels=self.normal_channels,
                                                   uniform_channels=self.uniform_channels)
            print(f"        latent space: mixture with uniform channels {self.uniform_channels}")

        self.build_inn()
        if self.bayesian:
            print(f"        Bayesian set to True, Bayesian layers: ", len(self.bayesian_layers))

    def get_constructor_func(self) -> Callable[[int, int], nn.Module]:
        """
        Returns a function that constructs a subnetwork with the given parameters

        Returns:
            Function that returns a subnet with input and output size as parameters
        """
        layer_class = VBLinear if self.bayesian else nn.Linear
        layer_args = {}
        if "prior_prec" in self.params:
            layer_args["prior_prec"] = self.params["prior_prec"]
        if "std_init" in self.params:
            layer_args["std_init"] = self.params["std_init"]

        def func(x_in: int, x_out: int) -> nn.Module:
            subnet = Subnet(
                self.params.get("layers_per_block", 3),
                x_in,
                x_out,
                internal_size=self.params.get("internal_size"),
                dropout=self.params.get("dropout", 0.0),
                layer_class=layer_class,
                layer_args=layer_args,
                bayesian_last=self.params.get("bayesian_last")
            )
            if self.bayesian:
                self.bayesian_layers.extend(
                    layer for layer in subnet.layer_list if isinstance(layer, VBLinear)
                )
            return subnet

        return func

    def get_coupling_block(self) -> tuple[Type, dict]:
        """
        Returns the class and keyword arguments for different coupling block types
        """
        constructor_fct = self.get_constructor_func()
        permute_soft = self.params.get("permute_soft", False)
        coupling_type = self.params.get("coupling_type", "affine")

        if coupling_type == "affine":
            print(f"        Coupling: affine")
            if self.latent_space == "uniform":
                raise ValueError("Affine couplings only support gaussian latent space")
            CouplingBlock = fm.AllInOneBlock
            block_kwargs = {
                "affine_clamping": self.params.get("clamping", 5.0),
                "subnet_constructor": constructor_fct,
                "global_affine_init": 0.92,
                "permute_soft": permute_soft,
            }
        elif coupling_type == "rational_quadratic":
            print(f"        Coupling: RQS")
            if self.latent_space == "gaussian":
                upper_bound = self.params.get("bounds", 10)
                lower_bound = -upper_bound
                left_bound = lower_bound
                right_bound = upper_bound
            elif self.latent_space == "uniform":
                lower_bound = 0
                upper_bound = 1
                right_bound = self.params.get("input_bound", 1)
                left_bound = -right_bound
                if permute_soft:
                    raise ValueError(
                        "Soft permutations not supported for uniform latent space"
                    )

            CouplingBlock = RationalQuadraticSplineBlock
            block_kwargs = {
                "num_bins": self.params.get("num_bins", 10),
                "subnet_constructor": constructor_fct,
                "left": left_bound,
                "right": right_bound,
                "bottom": lower_bound,
                "top": upper_bound,
                "permute_soft": permute_soft,
            }
        else:
            raise ValueError(f"Unknown coupling block type {coupling_type}")

        return CouplingBlock, block_kwargs

    def build_inn(self):
        """
        Construct the INN
        """
        self.madnis_inn = self.params.get("madnis_inn", False)
        bayesian_very_last = self.params.get("bayesian_very_last")
        if bayesian_very_last:
            print(f"    Using bayesian_very_last")
            self.params["bayesian_last"] = False
            self.bayesian = False

        if not self.madnis_inn:
            self.inn = ff.SequenceINN(self.dims_in)
            CouplingBlock, block_kwargs = self.get_coupling_block()
            for i in range(self.params.get("n_blocks", 10) - 1):
                self.inn.append(
                    CouplingBlock, cond=0, cond_shape=(self.dims_c,), **block_kwargs
                )

            if bayesian_very_last:
                self.params["bayesian_last"] = True
                self.bayesian = True
            CouplingBlock, block_kwargs = self.get_coupling_block()
            self.inn.append(
                CouplingBlock, cond=0, cond_shape=(self.dims_c,), **block_kwargs
            )
            return

        else:
            latent_space = self.params.get("latent_space", "gaussian")
            if latent_space != "gaussian":
                raise ValueError("Only gaussian latent space supported at the moment")

            subnet_meta = {"units": self.params.get("internal_size", 16),
                           "activation": self.params.get("activation", "relu"),
                           "layers": self.params.get("layers_per_block", 3),
                           "layer_constructor": VBLinear if self.bayesian else nn.Linear,
                           "prior_prec": self.params.get("prior_prec", 1),
                           "std_init": self.params.get("std_init", -9)}

            coupling_type = self.params.get("coupling_type", "rational_quadratic")
            if coupling_type == "rational_quadratic":
                coupling_block = RationalQuadraticSplineBlock
                coupling_block_kwargs = {"left": -1 * self.params.get("bounds", 10),
                                         "right": self.params.get("bounds", 10),
                                         "bottom": -1 * self.params.get("bounds", 10),
                                         "top": self.params.get("bounds", 10),
                                         "num_bins": self.params.get("num_bins", 10)}
            elif coupling_type == "affine":
                coupling_block = AffineCoupling
                coupling_block_kwargs = {"clamp": self.params.get("affine_clamp", 2)}
            else:
                raise ValueError(f"coupling_type {coupling_type} unknown")

            permutations = self.params.get("permutations", "soft")

            self.inn = FlowMapping(
                dims_in=self.dims_in,
                dims_c=self.dims_c,
                n_blocks=self.params.get("n_blocks", 10),
                subnet_constructor=MLP,
                subnet_meta=subnet_meta,
                coupling_block=coupling_block,
                coupling_kwargs=coupling_block_kwargs,
                permutations=permutations
            )


    def latent_log_prob(self, z: torch.Tensor) -> Union[torch.Tensor, float]:
        """
        Returns the log probability for a tensor in latent space

        Args:
            z: latent space tensor, shape (n_events, dims_in)
        Returns:
            log probabilities, shape (n_events, )
        """
        if self.latent_space == "gaussian":
            return -(z**2 / 2 + 0.5 * math.log(2 * math.pi)).sum(dim=1)
        elif self.latent_space == "uniform":
            return self.uniform_logprob

    def log_prob(self, x: torch.Tensor, c: torch.Tensor) -> torch.Tensor:
        """
        Evaluate the log probability

        Args:
            x: input tensor, shape (n_events, dims_in)
            c: condition tensor, shape (n_events, dims_c)
        Returns:
            log probabilities, shape (n_events, ) if not bayesian
        """
        jet_mask = torch.isnan(c)
        c_fixed = torch.where(jet_mask, 0, c)
        if not self.madnis_inn:
            z, jac = self.inn(x, (c_fixed,))
        else:
            z, jac = self.inn(x, c_fixed)
        return self.latent_log_prob(z) + jac

    def sample(self, c: torch.Tensor) -> torch.Tensor:
        """
        Generates samples and log probabilities for the given condition

        Args:
            c: condition tensor, shape (n_events, dims_c)
        Returns:
            x: generated samples, shape (n_events, dims_in)
            log_prob: log probabilites, shape (n_events, )
        """
        jet_mask = torch.isnan(c)
        c_fixed = torch.where(jet_mask, 0, c)
        z = self.latent_dist.sample((c.shape[0],)).to(c.device, dtype=c.dtype)
        if not self.madnis_inn:
            x, jac = self.inn(z, (c_fixed,), rev=True)
        else:
            x, jac = self.inn.inverse(z, c_fixed)
        return x

    def kl(self) -> torch.Tensor:
        """
        Compute the KL divergence between weight prior and posterior

        Returns:
            Scalar tensor with KL divergence
        """
        assert self.bayesian
        return sum(layer.kl() for layer in self.bayesian_layers)

    def batch_loss(
        self, x: torch.Tensor, c: torch.Tensor, kl_scale: float = 0.0
    ) -> tuple[torch.Tensor, dict]:
        """
        Evaluate the log probability

        Args:
            x: input tensor, shape (n_events, dims_in)
            c: condition tensor, shape (n_events, dims_c)
            kl_scale: factor in front of KL loss term, default 0
        Returns:
            loss: batch loss
            loss_terms: dictionary with loss contributions
        """
        inn_loss = -self.log_prob(x, c).mean() / self.dims_in
        if self.bayesian:
            kl_loss = kl_scale * self.kl() / self.dims_in
            loss = inn_loss + kl_loss * self.bayesian_factor
            loss_terms = {
                "loss": loss.item(),
                "nll": inn_loss.item(),
                "kl": kl_loss.item(),
            }
        else:
            loss = inn_loss
            loss_terms = {
                "loss": loss.item(),
            }
        return loss, loss_terms

    def reset_random_state(self):
        """
        Resets the random state of the Bayesian layers
        """
        assert self.bayesian
        for layer in self.bayesian_layers:
            layer.reset_random()

    def sample_random_state(self) -> list[np.ndarray]:
        """
        Sample new random states for the Bayesian layers and return them as a list

        Returns:
            List of numpy arrays with random states
        """
        assert self.bayesian
        return [layer.sample_random_state() for layer in self.bayesian_layers]

    def import_random_state(self, states: list[np.ndarray]):
        """
        Import a list of random states into the Bayesian layers

        Args:
            states: List of numpy arrays with random states
        """
        assert self.bayesian
        for layer, s in zip(self.bayesian_layers, states):
            layer.import_random_state(s)

    def generate_random_state(self):
        """
        Generate and save a set of random states for repeated use
        """
        assert self.bayesian
        self.random_states = [self.sample_random_state() for i in range(self.bayesian_samples)]
