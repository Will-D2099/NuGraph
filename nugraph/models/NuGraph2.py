from argparse import ArgumentParser
import warnings
import psutil

import torch
from torch import Tensor, cat, empty
from torch.optim import AdamW
from torch.optim.lr_scheduler import OneCycleLR
from pytorch_lightning import LightningModule
from torch_geometric.data import Batch, HeteroData
from torch_geometric.utils import unbatch

from .encoder import Encoder
from .plane import PlaneNet
from .nexus import NexusNet
from .decoders import SemanticDecoder, FilterDecoder, EventDecoder, VertexDecoder

class NuGraph2(LightningModule):
    """PyTorch Lightning module for model training.

    Wrap the base model in a LightningModule wrapper to handle training and
    inference, and compute training metrics."""
    def __init__(self,
                 in_features: int = 8,
                 planar_features: int = 64,
                 nexus_features: int = 16,
                 vertex_features: int = 32,
                 planes: list[str] = ['u','v','y'],
                 semantic_classes: list[str] = ['MIP','HIP','shower','michel','diffuse'],
                 event_classes: list[str] = ['numu','nue','nc'],
                 num_iters: int = 5,
                 event_head: bool = False,
                 semantic_head: bool = True,
                 filter_head: bool = True,
                 vertex_head: bool = False,
                 checkpoint: bool = False,
                 lr: float = 0.001):
        super().__init__()

        warnings.filterwarnings("ignore", ".*NaN values found in confusion matrix.*")

        self.save_hyperparameters()

        self.planes = planes
        self.semantic_classes = semantic_classes
        self.event_classes = event_classes
        self.num_iters = num_iters
        self.lr = lr

        self.encoder = Encoder(in_features,
                               planar_features,
                               planes,
                               semantic_classes)

        self.plane_net = PlaneNet(in_features,
                                  planar_features,
                                  len(semantic_classes),
                                  planes,
                                  checkpoint=checkpoint)

        self.nexus_net = NexusNet(planar_features,
                                  nexus_features,
                                  len(semantic_classes),
                                  planes,
                                  checkpoint=checkpoint)

        self.decoders = []

        if event_head:
            self.event_decoder = EventDecoder(
                planar_features,
                planes,
                semantic_classes,
                event_classes)
            self.decoders.append(self.event_decoder)

        if semantic_head:
            self.semantic_decoder = SemanticDecoder(
                planar_features,
                planes,
                semantic_classes)
            self.decoders.append(self.semantic_decoder)

        if filter_head:
            self.filter_decoder = FilterDecoder(
                planar_features,
                planes,
                semantic_classes)
            self.decoders.append(self.filter_decoder)
            
        if vertex_head:
            self.vertex_decoder = VertexDecoder(
                planar_features,
                vertex_features,
                planes,
                semantic_classes)
            self.decoders.append(self.vertex_decoder)

        if len(self.decoders) == 0:
            raise Exception('At least one decoder head must be enabled!')

    def forward(self,
                x: dict[str, Tensor],
                edge_index_plane: dict[str, Tensor],
                edge_index_nexus: dict[str, Tensor],
                nexus: Tensor,
                batch: dict[str, Tensor]) -> dict[str, Tensor]:
        m = self.encoder(x)
        for _ in range(self.num_iters):
            # shortcut connect features
            for i, p in enumerate(self.planes):
                s = x[p].detach().unsqueeze(1).expand(-1, m[p].size(1), -1)
                m[p] = torch.cat((m[p], s), dim=-1)
            self.plane_net(m, edge_index_plane)
            self.nexus_net(m, edge_index_nexus, nexus)
        ret = {}
        for decoder in self.decoders:
            ret.update(decoder(m, batch))
        return ret

    def step(self, data: HeteroData | Batch,
             stage: str = None,
             confusion: bool = False):

        # if it's a single data instance, convert to batch manually
        if isinstance(data, Batch):
            batch = data
        else:
            batch = Batch.from_data_list([data])

        # unpack tensors to pass into forward function
        x = self(batch.collect('x'),
                 { p: batch[p, 'plane', p].edge_index for p in self.planes },
                 { p: batch[p, 'nexus', 'sp'].edge_index for p in self.planes },
                 torch.empty(batch['sp'].num_nodes, 0),
                 { p: batch[p].batch for p in self.planes })

        # append output tensors back onto input data object
        if isinstance(data, Batch):
            dlist = [ HeteroData() for i in range(data.num_graphs) ]
            for attr, planes in x.items():
                for p, t in planes.items():
                    if t.size(0) == data[p].num_nodes:
                        tlist = unbatch(t, data[p].batch)
                    elif t.size(0) == data.num_graphs:
                        tlist = unbatch(t, torch.arange(data.num_graphs))
                    else:
                        raise Exception(f'don\'t know how to unbatch attribute {attr}')
                    for it_d, it_t in zip(dlist, tlist):
                        it_d[p][attr] = it_t
            tmp = Batch.from_data_list(dlist)
            data.update(tmp)
            for attr, planes in x.items():
                for p in planes:
                    data._slice_dict[p][attr] = tmp._slice_dict[p][attr]
                    data._inc_dict[p][attr] = tmp._inc_dict[p][attr]

        else:
            for key, value in x.items():
                data.set_value_dict(key, value)

        total_loss = 0.
        total_metrics = {}
        for decoder in self.decoders:
            loss, metrics = decoder.loss(data, stage, confusion)
            total_loss += loss
            total_metrics.update(metrics)
            decoder.finalize(data)

        return total_loss, total_metrics

    def on_train_start(self):
        hpmetrics = { 'max_lr': self.hparams.lr }
        self.logger.log_hyperparams(self.hparams, metrics=hpmetrics)
        self.max_mem_cpu = 0.
        self.max_mem_gpu = 0.

        scalars = {
            'loss': {'loss': [ 'Multiline', [ 'loss/train', 'loss/val' ]]},
            'acc': {}
        }
        for c in self.semantic_classes:
            scalars['acc'][c] = [ 'Multiline', [
                f'semantic_accuracy_class_train/{c}',
                f'semantic_accuracy_class_val/{c}'
            ]]
        self.logger.experiment.add_custom_scalars(scalars)

    def training_step(self,
                      batch,
                      batch_idx: int) -> float:
        loss, metrics = self.step(batch, 'train')
        self.log('loss/train', loss, batch_size=batch.num_graphs, prog_bar=True)
        self.log_dict(metrics, batch_size=batch.num_graphs)
        self.log_memory(batch, 'train')
        return loss

    def validation_step(self,
                        batch,
                        batch_idx: int) -> None:
        loss, metrics = self.step(batch, 'val', True)
        self.log('loss/val', loss, batch_size=batch.num_graphs)
        self.log_dict(metrics, batch_size=batch.num_graphs)

    def on_validation_epoch_end(self) -> None:
        epoch = self.trainer.current_epoch + 1
        for decoder in self.decoders:
            decoder.on_epoch_end(self.logger, 'val', epoch)

    def test_step(self,
                  batch,
                  batch_idx: int = 0) -> None:
        loss, metrics = self.step(batch, 'test', True)
        self.log('loss/test', loss, batch_size=batch.num_graphs)
        self.log_dict(metrics, batch_size=batch.num_graphs)
        self.log_memory(batch, 'test')

    def on_test_epoch_end(self) -> None:
        epoch = self.trainer.current_epoch + 1
        for decoder in self.decoders:
            decoder.on_epoch_end(self.logger, 'test', epoch)

    def predict_step(self,
                     batch: Batch,
                     batch_idx: int = 0) -> Batch:
        self.step(batch)
        return batch

    def configure_optimizers(self) -> tuple:
        optimizer = AdamW(self.parameters(),
                          lr=self.lr)
        onecycle = OneCycleLR(
                optimizer,
                max_lr=self.lr,
                total_steps=self.trainer.estimated_stepping_batches)
        return [optimizer], {'scheduler': onecycle, 'interval': 'step'}

    def log_memory(self, batch: Batch, stage: str) -> None:
        # log CPU memory
        if not hasattr(self, 'max_mem_cpu'):
            self.max_mem_cpu = 0.
        cpu_mem = psutil.Process().memory_info().rss / float(1073741824)
        self.max_mem_cpu = max(self.max_mem_cpu, cpu_mem)
        self.log(f'memory_cpu/{stage}', self.max_mem_cpu,
                 batch_size=batch.num_graphs, reduce_fx=torch.max)

        # log GPU memory
        if not hasattr(self, 'max_mem_gpu'):
            self.max_mem_gpu = 0.
        if self.device != torch.device('cpu'):
            gpu_mem = torch.cuda.memory_reserved(self.device)
            gpu_mem = float(gpu_mem) / float(1073741824)
            self.max_mem_gpu = max(self.max_mem_gpu, gpu_mem)
            self.log(f'memory_gpu/{stage}', self.max_mem_gpu,
                     batch_size=batch.num_graphs, reduce_fx=torch.max)

    @staticmethod
    def add_model_args(parser: ArgumentParser) -> ArgumentParser:
        '''Add argparse argpuments for model structure'''
        model = parser.add_argument_group('model', 'NuGraph2 model configuration')
        model.add_argument('--planar-feats', type=int, default=64,
                           help='Hidden dimensionality of planar convolutions')
        model.add_argument('--nexus-feats', type=int, default=16,
                           help='Hidden dimensionality of nexus convolutions')
        model.add_argument('--vertex-feats', type=int, default=32,
                           help='Hidden dimensionality of vertex decoder')
        model.add_argument('--event', action='store_true', default=False,
                           help='Enable event classification head')
        model.add_argument('--semantic', action='store_true', default=False,
                           help='Enable semantic segmentation head')
        model.add_argument('--filter', action='store_true', default=False,
                           help='Enable background filter head')
        model.add_argument('--vertex', action='store_true', default=False,
                           help='Enable vertex regression head')
        return parser

    @staticmethod
    def add_train_args(parser: ArgumentParser) -> ArgumentParser:
        train = parser.add_argument_group('train', 'NuGraph2 training configuration')
        train.add_argument('--no-checkpointing', action='store_true', default=False,
                           help='Disable checkpointing during training')
        train.add_argument('--epochs', type=int, default=80,
                           help='Maximum number of epochs to train for')
        train.add_argument('--learning-rate', type=float, default=0.001,
                           help='Max learning rate during training')
        train.add_argument('--clip-gradients', type=float, default=None,
                           help='Maximum value to clip gradient norm')
        train.add_argument('--gamma', type=float, default=2,
                           help='Focal loss gamma parameter')
        return parser
