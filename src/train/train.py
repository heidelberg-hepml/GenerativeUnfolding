from typing import Optional
from collections import defaultdict
from tqdm import tqdm
import time
from datetime import timedelta
import os
import torch
import numpy as np
from ema_pytorch import EMA
from ..models.inn import INN
from ..models.transfermer import Transfermer
from ..models.cfm import CFM, CFMwithTransformer
from ..models.didi import DirectDiffusion
from ..models.classifier import Classifier
from ..models.fff import FreeFormFlow
from .preprocessing import build_preprocessing, PreprocChain
from ..processes.base import Process, ProcessData
from .documenter import Documenter
from ..processes.zjets.process import ZJetsGenerative, ZJetsOmnifold


class Model:
    """
    Class for training, evaluating, loading and saving models for density estimation or
    importance sampling.
    """

    def __init__(
        self,
        params: dict,
        verbose: bool,
        device: torch.device,
        model_path: str,
        state_dict_attrs: list[str],
    ):
        """
        Initializes the training class.

        Args:
            params: Dictionary with run parameters
            verbose: print
            device: pytorch device
            model_path: path to save trained models and checkpoints in
            input_data: tensors with training, validation and test input data
            cond_data: tensors with training, validation and test condition data
            state_dict_attrs: list of attribute whose state dicts will be stored
        """
        self.params = params
        self.device = device
        self.model_path = model_path
        self.verbose = verbose
        self.is_classifier = False
        model = params.get("model", "INN")
        print(f"    Model class: {model}")
        try:
            self.model = eval(model)(params)
        except NameError:
            print(model)
            raise NameError("model not recognised. Use exact class name")
        self.model.to(device)

        n_params = sum(p.numel() for p in self.model.parameters() if p.requires_grad)
        print(f"    Total trainable parameters: {n_params}")
        #if hasattr(torch, "compile"):
        #    print("  Compiling model")
        #    self.mode = torch.compile(self.model)
        self.state_dict_attrs = [*state_dict_attrs, "model", "optimizer"]
        self.losses = defaultdict(list)

    def init_data_loaders(
        self,
        input_data: tuple[torch.Tensor, ...],
        cond_data: tuple[torch.Tensor, ...],
    ):
        input_train, input_val, input_test = input_data
        cond_train, cond_val, cond_test = cond_data
        self.n_train_samples = len(input_train)
        self.n_val_samples = len(input_val)
        self.bs = self.params.get("batch_size")
        self.bs_sample = self.params.get("batch_size_sample", self.bs)
        train_loader_kwargs = {"shuffle": True, "batch_size": self.bs, "drop_last": False}
        val_loader_kwargs = {"shuffle": False, "batch_size": self.bs_sample, "drop_last": False}

        self.train_loader = torch.utils.data.DataLoader(
            torch.utils.data.TensorDataset(input_train.float(), cond_train.float()),
            **train_loader_kwargs,
        )
        self.val_loader = torch.utils.data.DataLoader(
            torch.utils.data.TensorDataset(input_val.float(), cond_val.float()),
            **val_loader_kwargs,
        )
        self.test_loader = torch.utils.data.DataLoader(
            torch.utils.data.TensorDataset(input_test.float(), cond_test.float()),
            **val_loader_kwargs,
        )

    def progress(self, iterable, **kwargs):
        """
        Shows a progress bar if verbose training is enabled

        Args:
            iterable: iterable object
            kwargs: keyword arguments passed on to tqdm if verbose
        Returns:
            Unchanged iterable if not verbose, otherwise wrapped by tqdm
        """
        if self.verbose:
            return tqdm(iterable, **kwargs)
        else:
            return iterable

    def print(self, text):
        """
        Chooses print function depending on verbosity setting

        Args:
            text: String to be printed
        """
        if self.verbose:
            tqdm.write(text)
        else:
            print(text, flush=True)

    def init_optimizer(self):
        """
        Initialize optimizer and learning rate scheduling
        """
        optimizer = {
            "adam": torch.optim.Adam,
            "radam": torch.optim.RAdam,
        }[self.params.get("optimizer", "adam")]
        self.optimizer = optimizer(
            self.model.parameters(),
            lr=self.params.get("lr", 0.0002),
            betas=self.params.get("betas", [0.9, 0.999]),
            eps=self.params.get("eps", 1e-6),
            weight_decay=self.params.get("weight_decay", 0.0),
        )

        self.lr_sched_mode = self.params.get("lr_scheduler", None)
        if self.lr_sched_mode == "step":
            self.scheduler = torch.optim.lr_scheduler.StepLR(
                self.optimizer,
                step_size=self.params["lr_decay_epochs"],
                gamma=self.params["lr_decay_factor"],
            )
        elif self.lr_sched_mode == "one_cycle":
            self.scheduler = torch.optim.lr_scheduler.OneCycleLR(
                self.optimizer,
                self.params.get("max_lr", self.params["lr"] * 10),
                epochs=self.params["epochs"],
                steps_per_epoch=len(self.train_loader),
            )
        elif self.lr_sched_mode == "cosine_annealing":
            self.scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
                optimizer=self.optimizer,
                T_max= self.params["epochs"] * len(self.train_loader)
            )
        else:
            self.scheduler = None

        self.use_ema = self.params.get("use_ema", False)
        if self.use_ema:
            self.model.use_ema = True
            n_epochs = self.params.get("n_epochs", 100)
            trainloader_length = len(self.train_loader)
            ema_start = self.params.get("ema_start", 0.8)
            ema_start_iter = int(ema_start * n_epochs * trainloader_length)
            self.model.ema = EMA(self.model.net, update_after_step=ema_start_iter, update_every=10).to(self.device)
            print(f"    Using EMA with start at {ema_start}")

    def begin_epoch(self):
        """
        Overload this function to perform some task at the beginning of each epoch.
        """
        pass

    def train(self):
        """
        Main training loop
        """
        best_val_loss = 1e20
        checkpoint_interval = self.params.get("checkpoint_interval")
        checkpoint_overwrite = self.params.get("checkpoint_overwrite", True)
        use_ema = self.params.get("use_ema", False)

        start_time = time.time()
        for epoch in self.progress(
            range(self.params["epochs"]), desc="  Epoch", leave=False, position=0
        ):
            self.begin_epoch()
            self.model.train()
            epoch_train_losses = defaultdict(int)
            loss_scale = 1 / len(self.train_loader)
            for xs, cs in self.progress(
                self.train_loader, desc="  Batch", leave=False, position=1
            ):
                self.optimizer.zero_grad()
                loss, loss_terms = self.model.batch_loss(
                    xs, cs, 1 / self.n_train_samples
                )
                loss.backward()
                self.optimizer.step()
                if self.lr_sched_mode == "one_cycle" or self.lr_sched_mode == "cosine_annealing":
                    self.scheduler.step()
                for name, loss in loss_terms.items():
                    epoch_train_losses[name] += loss * loss_scale
                if use_ema:
                    self.model.ema.update()
            if self.lr_sched_mode == "step":
                self.scheduler.step()

            for name, loss in epoch_train_losses.items():
                self.losses[f"tr_{name}"].append(loss)
            for name, loss in self.dataset_loss(self.val_loader).items():
                self.losses[f"val_{name}"].append(loss)
            if epoch < 20:
                last_20_val_losses = self.losses["val_loss"]
            else:
                last_20_val_losses = self.losses["val_loss"][-20:]
            self.losses["val_movAvg"].append(torch.tensor(last_20_val_losses).mean().item())

            self.losses["lr"].append(self.optimizer.param_groups[0]["lr"])

            if self.losses["val_loss"][-1] < best_val_loss:
                best_val_loss = self.losses["val_loss"][-1]
                self.save("best")
            if (
                checkpoint_interval is not None
                and (epoch + 1) % checkpoint_interval == 0
            ):
                self.save("final" if checkpoint_overwrite else f"epoch_{epoch}")

            self.print(
                f"    Ep {epoch}: "
                + ", ".join(
                    [
                        f"{name} = {loss[-1]:{'.2e' if name == 'lr' else '.5f'}}"
                        for name, loss in self.losses.items()
                    ]
                )
                + f", t = {timedelta(seconds=round(time.time() - start_time))}"
            )

        self.save("final")
        time_diff = timedelta(seconds=round(time.time() - start_time))
        print(f"    Training completed after {time_diff}")

    def dataset_loss(self, loader: torch.utils.data.DataLoader) -> dict:
        """
        Computes the losses (without gradients) for the given data loader

        Args:
            loader: data loader
        Returns:
            Dictionary with loss terms averaged over all samples
        """
        self.model.eval()
        if self.model.bayesian:
            for layer in self.model.bayesian_layers:
                layer.map = True
            n_total = 0
            total_losses = defaultdict(list)
            with torch.no_grad():
                for xs, cs in self.progress(loader, desc="  Batch", leave=False):
                    n_samples = xs.shape[0]
                    n_total += n_samples
                    _, losses = self.model.batch_loss(
                        xs, cs, kl_scale=1 / self.n_train_samples
                    )
                    for name, loss in losses.items():
                        name = 'MAP_' + name
                        total_losses[name].append(loss * n_samples)

                for layer in self.model.bayesian_layers:
                    layer.map = False
                self.model.reset_random_state()

                n_total = 0
                for xs, cs in self.progress(loader, desc="  Batch", leave=False):
                    n_samples = xs.shape[0]
                    n_total += n_samples
                    _, losses = self.model.batch_loss(
                        xs, cs, kl_scale=1 / self.n_train_samples
                    )
                    for name, loss in losses.items():
                        total_losses[name].append(loss * n_samples)
        else:
            n_total = 0
            total_losses = defaultdict(list)
            with torch.no_grad():
                for xs, cs in self.progress(loader, desc="  Batch", leave=False):
                    n_samples = xs.shape[0]
                    n_total += n_samples
                    _, losses = self.model.batch_loss(
                        xs, cs, kl_scale=1 / self.n_train_samples
                    )
                    for name, loss in losses.items():
                        total_losses[name].append(loss * n_samples)
        return {name: sum(losses) / n_total for name, losses in total_losses.items()}

    def predict(self, loader=None) -> torch.Tensor:
        """
        Predict one sample for each event from the test data

        Returns:
            tensor with samples, shape (n_events, dims_in)
        """
        self.model.eval()
        bayesian_samples = self.params.get("bayesian_samples", 20) if self.model.bayesian else 1
        n_unfoldings = self.params.get("n_unfoldings", 1)
        
        #if self.model.bayesian and bayesian_samples > n_unfoldings:
        #    n_unfoldings = bayesian_samples
        #    bayesian_samples += 1 
        #elif self.model.bayesian and bayesian_samples <= n_unfoldings:
        #    bayesian_samples = n_unfoldings + 1
        #else:
        #    pass
        print("n_unfoldings: ", n_unfoldings)
        print("bayesian_samples: ", bayesian_samples)
        
        if loader is None:
            loader = self.test_loader

        with torch.no_grad():
            all_samples = []
            for i in range(bayesian_samples):
                unfoldings = []
                if self.model.bayesian:
                    t0 = time.time()
                    if i == 0:
                        for layer in self.model.bayesian_layers:
                            layer.map = True
                        for j in range(n_unfoldings):
                            tj0 = time.time()
                            data_batches = []
                            for xs, cs in self.progress(
                                loader,
                                desc="  Generating",
                                leave=False,
                                initial=i * len(loader),
                                total=bayesian_samples * len(loader),
                            ):
                                while True:
                                    try:
                                        data_batches.append(self.model.sample(cs))
                                        break
                                    except AssertionError:
                                        print(f"    Batch failed, repeating")
                            print(f"    Finished bayesian sample {i}, unfolding {j} in {time.time() - tj0}", flush=True)
                            unfoldings.append(torch.cat(data_batches, dim=0))
                    else:
                        for layer in self.model.bayesian_layers:
                            layer.map = False
                        self.model.reset_random_state()
                        data_batches = []
                        for xs, cs in self.progress(
                            loader,
                            desc="  Generating",
                            leave=False,
                            initial=i * len(loader),
                            total=bayesian_samples * len(loader),
                        ):
                            while True:
                                try:
                                    data_batches.append(self.model.sample(cs))
                                    break
                                except AssertionError:
                                    print(f"    Batch failed, repeating")
                        unfoldings.append(torch.cat(data_batches, dim=0))
                else:
                    for j in range(n_unfoldings):
                        tj0 = time.time()
                        data_batches = []
                        for xs, cs in self.progress(
                            loader,
                            desc="  Generating",
                            leave=False,
                            initial=i * len(loader),
                            total=bayesian_samples * len(loader),
                        ):
                            while True:
                                try:
                                    data_batches.append(self.model.sample(cs))
                                    break
                                except AssertionError:
                                    print(f"    Batch failed, repeating")
                        unfoldings.append(torch.cat(data_batches, dim=0))
                        print(f"    Finished unfolding {j} in {time.time() - tj0}", flush=True)

                unfoldings = torch.stack(unfoldings, dim=0)
                all_samples.append(unfoldings)
                if self.model.bayesian:
                    print(f"    Finished bayesian sample {i} in {time.time() - t0}", flush=True)
            all_samples = torch.cat(all_samples, dim=0)
            print("all samples shape:", all_samples.shape)
            #if self.model.bayesian:
            #    return all_samples#.reshape(bayesian_samples, -1, 6)
            #else:
            #    return all_samples#.reshape(-1, 6)
            return all_samples
        
    def predict_distribution(self, loader=None) -> torch.Tensor:
        """
        Predict multiple samples for a part of the test dataset

        Returns:
            tensor with samples, shape (n_events, n_samples, dims_in)
        """
        if loader is None:
            loader = self.test_loader
        self.model.eval()
        #bayesian_samples = self.params.get("bayesian_samples", 20) if self.model.bayesian else 1
        bayesian_samples = 1 if self.model.bayesian else 1
        max_batches = min(len(loader), self.params.get("max_dist_batches", 1))
        samples_per_event = self.params.get("dist_samples_per_event", 5)
        with torch.no_grad():
            all_samples = []
            for j in range(1):
                offset = j * max_batches * samples_per_event
                if self.model.bayesian:
                    for layer in self.model.bayesian_layers:
                            layer.map = True

                for i, (xs, cs) in enumerate(loader):
                    if i == max_batches:
                        break
                    data_batches = []
                    for _ in self.progress(
                        range(samples_per_event),
                        desc="  Generating",
                        leave=False,
                        initial=offset + i * samples_per_event,
                        total=bayesian_samples * max_batches * samples_per_event,
                    ):
                        while True:
                            try:
                                data_batches.append(self.model.sample(cs))
                                break
                            except AssertionError:
                                print("Batch failed, repeating")
                    all_samples.append(torch.stack(data_batches, dim=1))
            all_samples = torch.cat(all_samples, dim=0)
            if self.model.bayesian:
                return all_samples.reshape(
                    bayesian_samples,
                    len(all_samples) // bayesian_samples,
                    *all_samples.shape[1:],
                )
            else:
                return all_samples
    
    def train_classifier(self,
                         doc: Documenter,
                         raw_data: torch.Tensor,
                         raw_gen_single: torch.Tensor,
                         raw_gen_dist: torch.Tensor,
                         permutation: torch.Tensor) -> torch.Tensor:
        """
        Train a classifier
        """
        print("------- Running classifier training -------")
        raw_gen_single = raw_gen_single[0] if self.model.bayesian else raw_gen_single
        raw_gen_single[:, 2] = torch.round(raw_gen_single[:, 2]) # round jet multiplicity
        n_events = len(raw_data.x_hard)
        
        use_cuda = torch.cuda.is_available()
        print("Using device " + ("GPU" if use_cuda else "CPU"))
        device = torch.device("cuda:0" if use_cuda else "cpu")
        
        label = [torch.ones((len(raw_data.x_hard), 1)), torch.zeros((len(raw_gen_single), 1))]
        label = torch.cat(label).to(torch.float32).to(self.device)
        x_hard = torch.cat((raw_data.x_hard, raw_gen_single)).to(torch.float32).to(self.device)
                
        n_events = len(x_hard)
        assert len(label) == n_events
        assert n_events == len(permutation)

        
        x_hard = x_hard[permutation]
        label = label[permutation]
        # Load data
        data_train = {}
        data_val = {}
        data_test = {}
        classifier_params = self.params["classifier_params"]
        process_params = classifier_params["process_params"]

        trlow, trhigh = process_params["train_slice"]
        valow, vahigh = process_params["val_slice"]
        telow, tehigh = process_params["test_slice"]

        tr_data_slice = slice(int(n_events * trlow), int(n_events * trhigh))
        va_data_slice = slice(int(n_events * valow), int(n_events * vahigh))
        te_data_slice = slice(int(n_events * telow), int(n_events * tehigh))

        for name, var in zip(["x_hard", "label"], [x_hard, label]):
            data_train[name] = var[tr_data_slice]
            data_val[name] = var[va_data_slice]
            data_test[name] = var[te_data_slice]                              

        # Init model
        dims_in = data_train["label"].shape[1]
        dims_c = data_train["x_hard"].shape[1]
        classifier_params["dims_in"] = dims_in
        classifier_params["dims_c"] = dims_c
        print(f"    Train events: {len(data_train['x_hard'])}")
        print(f"    Val events: {len(data_val['x_hard'])}")
        print(f"    Test events: {len(data_test['x_hard'])}")
        print(f"    Hard dimension: {dims_c}")
        print(f"    Label dimension: {dims_in}")
        classifier_path = doc.get_file("classifier", False)
        os.makedirs(classifier_path, exist_ok=True)
        print("------- Building classifier -------")
        classifier = Classifier(classifier_params).to(device)

        # init dataloaders
        input_train, input_val, input_test = data_train["label"], data_val["label"], data_test["label"]
        cond_train, cond_val, cond_test = data_train["x_hard"], data_val["x_hard"], data_test["x_hard"]
        n_train_samples = len(input_train)
        n_val_samples = len(input_val)
        bs = classifier_params.get("batch_size")
        bs_sample = classifier_params.get("batch_size_sample", bs)
        train_loader_kwargs = {"shuffle": True, "batch_size": bs, "drop_last": False}
        val_loader_kwargs = {"shuffle": False, "batch_size": bs_sample, "drop_last": False}

        train_loader = torch.utils.data.DataLoader(
            torch.utils.data.TensorDataset(input_train.float(), cond_train.float()),
            **train_loader_kwargs,
        )
        val_loader = torch.utils.data.DataLoader(
            torch.utils.data.TensorDataset(input_val.float(), cond_val.float()),
            **val_loader_kwargs,
        )


        # init Optimizer
        optim = {
            "adam": torch.optim.Adam,
            "radam": torch.optim.RAdam,
        }[self.params.get("optimizer", "adam")]
        optimizer = optim(
            classifier.parameters(),
            lr=classifier_params.get("lr", 0.0002),
            betas=classifier_params.get("betas", [0.9, 0.999]),
            eps=classifier_params.get("eps", 1e-6),
            weight_decay=classifier_params.get("weight_decay", 0.0),
        )

        lr_sched_mode = classifier_params.get("lr_scheduler", None)
        if lr_sched_mode == "step":
            scheduler = torch.optim.lr_scheduler.StepLR(
                optimizer,
                step_size=classifier_params["lr_decay_epochs"],
                gamma=classifier_params["lr_decay_factor"],
            )
        elif lr_sched_mode == "one_cycle":
            scheduler = torch.optim.lr_scheduler.OneCycleLR(
                optimizer,
                classifier_params.get("max_lr", classifier_params["lr"] * 10),
                epochs=classifier_params["epochs"],
                steps_per_epoch=len(train_loader),
            )
        elif lr_sched_mode == "cosine_annealing":
            scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
                optimizer=optimizer,
                T_max= classifier_params["epochs"] * len(train_loader)
            )
        else:
            scheduler = None


        # Training loop
        best_val_loss = 1e20
        checkpoint_interval = classifier_params.get("checkpoint_interval")
        checkpoint_overwrite = self.params.get("checkpoint_overwrite", True)
        use_ema = False

        state_dict_attrs = ['model']
        cl_losses = defaultdict(list)

        start_time = time.time()
        for epoch in self.progress(
            range(classifier_params["epochs"]), desc="  Epoch", leave=False, position=0
        ):
            # Train
            #self.begin_epoch()
            classifier.train()
            epoch_train_losses = defaultdict(int)
            loss_scale = 1 / len(train_loader)
            for xs, cs in self.progress(
                train_loader, desc="  Batch", leave=False, position=1
            ):
                optimizer.zero_grad()
                loss, loss_terms = classifier.batch_loss(
                    xs, cs, 1 / n_train_samples
                )
                loss.backward()
                optimizer.step()
                if lr_sched_mode == "one_cycle" or lr_sched_mode == "cosine_annealing":
                    scheduler.step()
                for name, loss in loss_terms.items():
                    epoch_train_losses[name] += loss * loss_scale
                if use_ema:
                    classifier.ema.update()
            if lr_sched_mode == "step":
                scheduler.step()

            for name, loss in epoch_train_losses.items():
                cl_losses[f"tr_{name}"].append(loss)

            # Evaluate
            classifier.eval()
            if classifier.bayesian:
                for layer in classifier.bayesian_layers:
                    layer.map = True
                n_total = 0
                total_losses = defaultdict(list)
                with torch.no_grad():
                    for xs, cs in self.progress(val_loader, desc="  Batch", leave=False):
                        n_samples = xs.shape[0]
                        n_total += n_samples
                        _, losses = classifier.batch_loss(
                            xs, cs, kl_scale=1 / n_train_samples
                        )
                        for name, loss in losses.items():
                            name = 'MAP_' + name
                            total_losses[name].append(loss * n_samples)

                    for layer in classifier.bayesian_layers:
                        layer.map = False
                    classifier.reset_random_state()

                    n_total = 0
                    for xs, cs in self.progress(val_loader, desc="  Batch", leave=False):
                        n_samples = xs.shape[0]
                        n_total += n_samples
                        _, losses = classifier.batch_loss(
                            xs, cs, kl_scale=1 / n_train_samples
                        )
                        for name, loss in losses.items():
                            total_losses[name].append(loss * n_samples)
            else:
                n_total = 0
                total_losses = defaultdict(list)
                with torch.no_grad():
                    for xs, cs in self.progress(val_loader, desc="  Batch", leave=False):
                        n_samples = xs.shape[0]
                        n_total += n_samples
                        _, losses = classifier.batch_loss(
                            xs, cs, kl_scale=1 / n_train_samples
                        )
                        for name, loss in losses.items():
                            total_losses[name].append(loss * n_samples)
            cl_dataset_loss =  {name: sum(losses) / n_total for name, losses in total_losses.items()}
            
            for name, loss in cl_dataset_loss.items():
                cl_losses[f"val_{name}"].append(loss)
            if epoch < 20:
                last_20_val_losses = cl_losses["val_loss"]
            else:
                last_20_val_losses = cl_losses["val_loss"][-20:]
            cl_losses["val_movAvg"].append(torch.tensor(last_20_val_losses).mean().item())

            cl_losses["lr"].append(optimizer.param_groups[0]["lr"])

    
            if cl_losses["val_loss"][-1] < best_val_loss:
                best_val_loss = cl_losses["val_loss"][-1]
                torch.save(
                    {
                        **{
                        attr: getattr(classifier, attr).state_dict()
                        for attr in state_dict_attrs
                        },
                        "losses": cl_losses
                    },
                    classifier_path + "/best.pth",
                )
            if (
                checkpoint_interval is not None
                and (epoch + 1) % checkpoint_interval == 0
            ):
                
                torch.save(
                    {
                        **{
                        attr: getattr(classifier, attr).state_dict()
                        for attr in state_dict_attrs
                        },
                        "losses": cl_losses
                    },
                    classifier_path + "/final.pth" if checkpoint_overwrite else classifier_path + "/epoch_{epoch}.pth"
                )

            self.print(
                f"    Ep {epoch}: "
                + ", ".join(
                    [
                        f"{name} = {loss[-1]:{'.2e' if name == 'lr' else '.5f'}}"
                        for name, loss in cl_losses.items()
                    ]
                )
                + f", t = {timedelta(seconds=round(time.time() - start_time))}"
            )

        torch.save(
            {
                **{
                    attr: getattr(classifier, attr).state_dict()
                    for attr in state_dict_attrs
                },
                "losses": cl_losses
            },
            classifier_path + "/final.pth",
        )
        print(classifier)
        time_diff = timedelta(seconds=round(time.time() - start_time))
        print(f"    Classifier training completed after {time_diff}")

    def eval_classifier(self,
                         doc: Documenter,
                         raw_data: torch.Tensor,
                         raw_gen_single: torch.Tensor,
                         raw_gen_dist: torch.Tensor,
                         permutation: torch.Tensor,
                         data: str = "test",
                         model_name: str = "final",
                         name: str = "") -> torch.Tensor:
        """
        Evaluate the classifier
        """
        print("------- Running classifier evaluation -------")
        print(f"Checkpoint: {model_name},  Data: {data}")
        raw_gen_single = raw_gen_single[0] if self.model.bayesian else raw_gen_single
        raw_gen_single[:, 2] = torch.round(raw_gen_single[:, 2]) # round jet multiplicity
        n_events = len(raw_data.x_hard)
        
        use_cuda = torch.cuda.is_available()
        print("Using device " + ("GPU" if use_cuda else "CPU"))
        device = torch.device("cuda:0" if use_cuda else "cpu")
        
        label = [torch.ones((len(raw_data.x_hard), 1)), torch.zeros((len(raw_gen_single), 1))]
        label = torch.cat(label).to(torch.float32).to(device)
        x_hard = torch.cat((raw_data.x_hard, raw_gen_single)).to(torch.float32).to(device)
                
        n_events = len(x_hard)
        assert len(label) == n_events
        
        x_hard = x_hard[permutation]
        label = label[permutation]
        # Load data
        data_train = {}
        data_val = {}
        data_test = {}
        classifier_params = self.params["classifier_params"]
        process_params = classifier_params["process_params"]
        state_dict_attrs = ['model']

        trlow, trhigh = process_params["train_slice"]
        valow, vahigh = process_params["val_slice"]
        telow, tehigh = process_params["test_slice"]

        tr_data_slice = slice(int(n_events * trlow), int(n_events * trhigh))
        va_data_slice = slice(int(n_events * valow), int(n_events * vahigh))
        te_data_slice = slice(int(n_events * telow), int(n_events * tehigh))

        for name, var in zip(["x_hard", "label"], [x_hard, label]):
            data_train[name] = var[tr_data_slice]
            data_val[name] = var[va_data_slice]
            data_test[name] = var[te_data_slice]                             

        if data == "test":
            dims_in = data_test["label"].shape[1]
            dims_c = data_test["x_hard"].shape[1]
        elif data == "train":
            dims_in = data_train["label"].shape[1]
            dims_c = data_train["x_hard"].shape[1]
        else:
            pass
        classifier_params["dims_in"] = dims_in
        classifier_params["dims_c"] = dims_c

        # init dataloaders
        input_train, input_val, input_test = data_train["label"], data_val["label"], data_test["label"]
        cond_train, cond_val, cond_test = data_train["x_hard"], data_val["x_hard"], data_test["x_hard"]
        n_train_samples = len(input_train)
        n_val_samples = len(input_val)
        bs = classifier_params.get("batch_size")
        bs_sample = classifier_params.get("batch_size_sample", bs)
        train_loader_kwargs = {"shuffle": False, "batch_size": 10 * bs, "drop_last": False}
        val_loader_kwargs = {"shuffle": False, "batch_size": bs_sample, "drop_last": False}

        if data == "test":
            loader = torch.utils.data.DataLoader(
                torch.utils.data.TensorDataset(input_test.float(),
                                               cond_test.float()),
                                               **val_loader_kwargs
                                               )
        elif data == "train":
            loader = torch.utils.data.DataLoader(
                torch.utils.data.TensorDataset(input_train.float(),
                                               cond_train.float()),
                                               **train_loader_kwargs,
                                               )
        classifier = Classifier(classifier_params).to(device)
        try:
            state_dict = torch.load(doc.get_file(f"classifier/{model_name}.pth", False), map_location=device)
            for attr in state_dict_attrs:
                try:
                    getattr(classifier, attr).load_state_dict(state_dict[attr])
                    print(f"    Loaded {attr}")
                except AttributeError:
                    print(f"    Could not load {attr}")
                    pass
            self.cl_losses = state_dict["losses"]
        except FileNotFoundError:
            raise FileNotFoundError("Classifier model not found")
        
        print(f"    Predicting weights")
        t0 = time.time()
        # predict probabilities
        classifier.eval()

        if loader is None:
            raise ValueError("No data loader provided")

        bayesian_samples = classifier_params.get("bayesian_samples", 20) if classifier.bayesian else 1
        with torch.no_grad():
            all_samples = []
            for i in range(bayesian_samples):
                if classifier.bayesian:
                    if i == 0:
                        for layer in classifier.bayesian_layers:
                            layer.map = True
                    else:
                        for layer in classifier.bayesian_layers:
                            layer.map = False
                        classifier.reset_random_state()
                predictions = []
                t0 = time.time()
                for xs, cs in self.progress(loader, desc="  Predicting", leave=False):
                    predictions.append(classifier.probs(cs))
                all_samples.append(torch.cat(predictions, dim=0))
                if classifier.bayesian:
                    print(f"    Finished bayesian sample {i} in {time.time() - t0}", flush=True)
            all_samples = torch.cat(all_samples, dim=0)
        if classifier.bayesian:
            predictions = all_samples.reshape(
                bayesian_samples,
                len(all_samples) // bayesian_samples,
                *all_samples.shape[1:],
            )
        else:
            predictions = all_samples
        t1 = time.time()
        time_diff = timedelta(seconds=round(t1 - t0))
        print(f"    Predictions completed after {time_diff}")

        if classifier_params.get("compute_test_loss", False):
            print(f"    Computing {data} loss")
            # Evaluate
            classifier.eval()
            if classifier.bayesian:
                for layer in classifier.bayesian_layers:
                    layer.map = True
                n_total = 0
                total_losses = defaultdict(list)
                with torch.no_grad():
                    for xs, cs in self.progress(loader, desc="  Batch", leave=False):
                        n_samples = xs.shape[0]
                        n_total += n_samples
                        _, losses = classifier.batch_loss(
                            xs, cs, kl_scale=1 / n_train_samples
                        )
                        for name, loss in losses.items():
                            name = 'MAP_' + name
                            total_losses[name].append(loss * n_samples)

                    for layer in classifier.bayesian_layers:
                        layer.map = False
                    classifier.reset_random_state()

                    n_total = 0
                    for xs, cs in self.progress(loader, desc="  Batch", leave=False):
                        n_samples = xs.shape[0]
                        n_total += n_samples
                        _, losses = classifier.batch_loss(
                            xs, cs, kl_scale=1 / n_train_samples
                        )
                        for name, loss in losses.items():
                            total_losses[name].append(loss * n_samples)
            else:
                n_total = 0
                total_losses = defaultdict(list)
                with torch.no_grad():
                    for xs, cs in self.progress(loader, desc="  Batch", leave=False):
                        n_samples = xs.shape[0]
                        n_total += n_samples
                        _, losses = classifier.batch_loss(
                            xs, cs, kl_scale=1 / n_train_samples
                        )
                        for name, loss in losses.items():
                            total_losses[name].append(loss * n_samples)
            cl_dataset_loss =  {name: sum(losses) / n_total for name, losses in total_losses.items()}
            print(f"    Result: {cl_dataset_loss['loss']:.4f}")
        return predictions, cond_test, input_test

    def save(self, name: str):
        """
        Saves the model, preprocessing, optimizer and losses.

        Args:
            name: File name for the model (without path and extension)
        """
        file = os.path.join(self.model_path, f"{name}.pth")
        torch.save(
            {
                **{
                    attr: getattr(self, attr).state_dict()
                    for attr in self.state_dict_attrs
                },
                "losses": self.losses,
            },
            file,
        )
        if self.use_ema:
            file = os.path.join(self.model_path, f"ema_{name}.pth")
            torch.save(self.model.ema.state_dict(), file)

    def load(self, name: str):
        """
        Loads the model, preprocessing, optimizer and losses.

        Args:
            name: File name for the model (without path and extension)
        """
        file = os.path.join(self.model_path, f"{name}.pth")
        state_dicts = torch.load(file, map_location=self.device)
        for attr in self.state_dict_attrs:
            try:
                getattr(self, attr).load_state_dict(state_dicts[attr])
            except AttributeError:
                pass
        self.losses = state_dicts["losses"]

        if self.params.get("use_ema", False):
            self.use_ema = True
            self.model.use_ema = True
            file = os.path.join(self.model_path, f"ema_{name}.pth")
            ema_dict = torch.load(file, map_location=self.device)
            self.model.ema = EMA(self.model.net).to(self.device)
            self.model.ema.load_state_dict(ema_dict)


class GenerativeUnfolding(Model):
    def __init__(
        self,
        params: dict,
        verbose: bool,
        device: torch.device,
        model_path: str,
        process: Process
    ):
        self.process = process

        self.hard_pp = build_preprocessing(params.get("hard_preprocessing", {}), n_dim=params["dims_in"])
        self.reco_pp = build_preprocessing(params.get("reco_preprocessing", {}), n_dim=params["dims_c"])
        self.hard_pp.to(device)
        self.reco_pp.to(device)
        self.latent_dimension = self.hard_pp.output_shape[0]

        super().__init__(
            params,
            verbose,
            device,
            model_path,
            state_dict_attrs=["hard_pp", "reco_pp"],
        )

        self.unpaired = params.get("unpaired", False)
        if self.unpaired:
            assert isinstance(self.model, DirectDiffusion)
            print(f"    Using unpaired data")

    def init_data_loaders(self):
        data = (
        self.process.get_data("train"),
        self.process.get_data("val"),
        self.process.get_data("test"),
        )
        if self.params.get("joint_normalization", False):
            self.hard_pp.init_normalization(data[0].x_hard)
            self.reco_pp.init_normalization(data[0].x_hard)
        else:
            self.hard_pp.init_normalization(data[0].x_hard)
            self.reco_pp.init_normalization(data[0].x_reco)
        self.input_data_preprocessed = tuple(self.hard_pp(subset.x_hard) for subset in data)
        self.cond_data_preprocessed = tuple(self.reco_pp(subset.x_reco) for subset in data)
        super(GenerativeUnfolding, self).init_data_loaders(self.input_data_preprocessed, self.cond_data_preprocessed)

    def begin_epoch(self):
        # The only difference between paired and unpaired is shuffling the condition data each epoch
        if not self.unpaired:
            return

        train_loader_kwargs = {"shuffle": True, "batch_size": self.bs, "drop_last": False}
        val_loader_kwargs = {"shuffle": False, "batch_size": self.bs_sample, "drop_last": False}

        input_train = self.input_data_preprocessed[0].clone()
        cond_train = self.cond_data_preprocessed[0].clone()
        input_val = self.input_data_preprocessed[1].clone()
        cond_val = self.cond_data_preprocessed[1].clone()

        permutation_train = torch.randperm(self.n_train_samples)
        permutation_val = torch.randperm(self.n_val_samples)
        cond_train = cond_train[permutation_train]
        cond_val = cond_val[permutation_val]

        self.train_loader = torch.utils.data.DataLoader(
            torch.utils.data.TensorDataset(input_train.float(), cond_train.float()),
            **train_loader_kwargs,
        )
        self.val_loader = torch.utils.data.DataLoader(
            torch.utils.data.TensorDataset(input_val.float(), cond_val.float()),
            **val_loader_kwargs,
        )

    def predict(self, loader=None) -> torch.Tensor:
        """
        Predict one sample for each event in the test dataset

        Returns:
            tensor with samples, shape (n_events, dims_in)
        """
        samples = super().predict(loader)
        #samples = self.input_data_preprocessed[2] # use this to check that predicted = just the true data recovered inverting the preproc
        samples_pp = self.hard_pp(
            samples.reshape(-1, samples.shape[-1]), rev=True, batch_size=1000
        )
        return samples_pp.reshape(*samples.shape[:-1], *samples_pp.shape[1:])

    def predict_distribution(self, loader=None) -> torch.Tensor:
        """
        Predict multiple samples for a part of the test dataset

        Returns:
            tensor with samples, shape (n_events, n_samples, dims_in)
        """
        samples = super().predict_distribution(loader)
        samples_pp = self.hard_pp(
            samples.reshape(-1, samples.shape[-1]),
            rev=True,
            batch_size=1000,
        )
        if self.model.bayesian:
            return samples_pp.reshape(*samples.shape[:3], *samples_pp.shape[1:])
        else:
            return samples_pp.reshape(*samples.shape[:2], *samples_pp.shape[1:])


class Omnifold(Model):
    def __init__(
        self,
        params: dict,
        verbose: bool,
        device: torch.device,
        model_path: str,
        process: Process
    ):
        self.process = process

        self.hard_pp = build_preprocessing(params["hard_preprocessing"], n_dim=params["dims_c"])
        self.reco_pp = build_preprocessing(params["reco_preprocessing"], n_dim=params["dims_in"])
        self.hard_pp.to(device)
        self.reco_pp.to(device)
        self.latent_dimension = self.hard_pp.output_shape[0]
        self.params = params

        super().__init__(
            params,
            verbose,
            device,
            model_path,
            state_dict_attrs=["hard_pp", "reco_pp"],
        )

    def init_data_loaders(self):
        data = (
        self.process.get_data("train"),
        self.process.get_data("val"),
        self.process.get_data("test"),
        )
        label_data = tuple(subset.label for subset in data)
        self.reco_pp.init_normalization(data[0].x_reco)
        reco_data = tuple(self.reco_pp(subset.x_reco) for subset in data)
        super(Omnifold, self).init_data_loaders(label_data, reco_data)

    def predict_probs(self, loader=None):
        self.model.eval()

        if loader is None:
            loader = self.test_loader

        bayesian_samples = self.params.get("bayesian_samples", 20) if self.model.bayesian else 1
        with torch.no_grad():
            all_samples = []
            for i in range(bayesian_samples):
                if self.model.bayesian:
                    if i == 0:
                        for layer in self.model.bayesian_layers:
                            layer.map = True
                    else:
                        for layer in self.model.bayesian_layers:
                            layer.map = False
                        self.model.reset_random_state()
                predictions = []
                t0 = time.time()
                for xs, cs in self.progress(loader, desc="  Predicting", leave=False):
                    predictions.append(self.model.probs(cs))
                all_samples.append(torch.cat(predictions, dim=0))
                if self.model.bayesian:
                    print(f"    Finished bayesian sample {i} in {time.time() - t0}", flush=True)
            all_samples = torch.cat(all_samples, dim=0)
        if self.model.bayesian:
            return all_samples.reshape(
                bayesian_samples,
                len(all_samples) // bayesian_samples,
                *all_samples.shape[1:],
            )
        else:
            return all_samples

