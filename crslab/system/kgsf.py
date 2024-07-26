# @Time   : 2020/11/22
# @Author : Kun Zhou
# @Email  : francis_kun_zhou@163.com

# UPDATE:
# @Time   : 2020/11/24, 2021/1/3
# @Author : Kun Zhou, Xiaolei Wang
# @Email  : francis_kun_zhou@163.com, wxl1999@foxmail.com

import os
import torch
from loguru import logger

from crslab.evaluator.metrics.base import AverageMetric
from crslab.evaluator.metrics.gen import PPLMetric
from crslab.system.base import BaseSystem
from crslab.system.utils.functions import ind2txt


class KGSFSystem(BaseSystem):
    """System implementation for the KGSF model."""

    def __init__(self, opt, train_dataloader, valid_dataloader, test_dataloader, vocab, side_data, 
                 restore_system=False, interact=False, debug=False, tensorboard=False):
        """
        Args:
            opt (dict): Hyperparameters.
            train_dataloader (BaseDataLoader): Training dataloader.
            valid_dataloader (BaseDataLoader): Validation dataloader.
            test_dataloader (BaseDataLoader): Testing dataloader.
            vocab (dict): Vocabulary data.
            side_data (dict): Additional data.
            restore_system (bool): Whether to restore the system state after training. Default: False.
            interact (bool): Whether to enable interaction mode. Default: False.
            debug (bool): Whether to run in debug mode. Default: False.
            tensorboard (bool): Whether to use TensorBoard for monitoring. Default: False.
        """
        super(KGSFSystem, self).__init__(opt, train_dataloader, valid_dataloader, test_dataloader, 
                                         vocab, side_data, restore_system, interact, debug, tensorboard)

        self.ind2tok = vocab['ind2tok']
        self.end_token_idx = vocab['end']
        self.item_ids = side_data['item_entity_ids']

        self.pretrain_optim_opt = self.opt['pretrain']
        self.rec_optim_opt = self.opt['rec']
        self.conv_optim_opt = self.opt['conv']
        self.pretrain_epoch = self.pretrain_optim_opt['epoch']
        self.rec_epoch = self.rec_optim_opt['epoch']
        self.conv_epoch = self.conv_optim_opt['epoch']
        self.pretrain_batch_size = self.pretrain_optim_opt['batch_size']
        self.rec_batch_size = self.rec_optim_opt['batch_size']
        self.conv_batch_size = self.conv_optim_opt['batch_size']

    def rec_evaluate(self, rec_predict, item_label):
        rec_predict = rec_predict.cpu()
        rec_predict = rec_predict[:, self.item_ids]
        _, rec_ranks = torch.topk(rec_predict, 50, dim=-1)
        rec_ranks = rec_ranks.tolist()
        item_label = item_label.tolist()
        for rec_rank, item in zip(rec_ranks, item_label):
            item = self.item_ids.index(item)
            self.evaluator.rec_evaluate(rec_rank, item)

    def conv_evaluate(self, prediction, response):
        prediction = prediction.tolist()
        response = response.tolist()
        for p, r in zip(prediction, response):
            p_str = ind2txt(p, self.ind2tok, self.end_token_idx)
            r_str = ind2txt(r, self.ind2tok, self.end_token_idx)
            self.evaluator.gen_evaluate(p_str, [r_str])

    def step(self, batch, stage, mode):
        batch = [ele.to(self.device) for ele in batch]
        
        if stage == 'pretrain':
            self._handle_pretrain_stage(batch, mode)
        elif stage == 'rec':
            self._handle_rec_stage(batch, mode)
        elif stage == 'conv':
            self._handle_conv_stage(batch, mode)
        else:
            raise ValueError(f"Invalid stage provided: {stage}")

    def _handle_pretrain_stage(self, batch, mode):
        info_loss = self.model.forward(batch, 'pretrain', mode)
        if info_loss is not None:
            self.backward(info_loss.sum())
            info_loss = info_loss.sum().item()
            self.evaluator.optim_metrics.add("info_loss", AverageMetric(info_loss))

    def _handle_rec_stage(self, batch, mode):
        rec_loss, info_loss, rec_predict = self.model.forward(batch, 'rec', mode)
        loss = rec_loss + (0.025 * info_loss if info_loss else 0)
        
        if mode == "train":
            self.backward(loss.sum())
        else:
            self.rec_evaluate(rec_predict, batch[-1])
        
        self.evaluator.optim_metrics.add("rec_loss", AverageMetric(rec_loss.sum().item()))
        if info_loss:
            self.evaluator.optim_metrics.add("info_loss", AverageMetric(info_loss.sum().item()))

    def _handle_conv_stage(self, batch, mode):
        if mode == "test":
            pred = self.model.forward(batch, 'conv', mode)
            self.conv_evaluate(pred, batch[-1])
        else:
            gen_loss, pred = self.model.forward(batch, 'conv', mode)
            if mode == 'train':
                self.backward(gen_loss.sum())
            else:
                self.conv_evaluate(pred, batch[-1])
            self.evaluator.optim_metrics.add("gen_loss", AverageMetric(gen_loss.sum().item()))
            self.evaluator.gen_metrics.add("ppl", PPLMetric(gen_loss.sum().item()))

    def pretrain(self):
        self.init_optim(self.pretrain_optim_opt, self.model.parameters())

        for epoch in range(self.pretrain_epoch):
            self.evaluator.reset_metrics()
            logger.info(f'[Pretrain epoch {epoch}]')
            for batch in self.train_dataloader.get_pretrain_data(self.pretrain_batch_size, shuffle=False):
                self.step(batch, stage="pretrain", mode='train')
            self.evaluator.report()

    def train_recommender(self):
        self.init_optim(self.rec_optim_opt, self.model.parameters())

        for epoch in range(self.rec_epoch):
            self.evaluator.reset_metrics()
            logger.info(f'[Recommendation epoch {epoch}]')
            logger.info('[Train]')
            for batch in self.train_dataloader.get_rec_data(self.rec_batch_size, shuffle=False):
                self.step(batch, stage='rec', mode='train')
            self.evaluator.report(epoch=epoch, mode='train')

            logger.info('[Valid]')
            with torch.no_grad():
                self.evaluator.reset_metrics()
                for batch in self.valid_dataloader.get_rec_data(self.rec_batch_size, shuffle=False):
                    self.step(batch, stage='rec', mode='val')
                self.evaluator.report(epoch=epoch, mode='val')
                
                metric = self.evaluator.rec_metrics['hit@1'] + self.evaluator.rec_metrics['hit@50']
                if self.early_stop(metric):
                    break

        logger.info('[Test]')
        with torch.no_grad():
            self.evaluator.reset_metrics()
            for batch in self.test_dataloader.get_rec_data(self.rec_batch_size, shuffle=False):
                self.step(batch, stage='rec', mode='test')
            self.evaluator.report(mode='test')

    def train_conversation(self):
        if os.environ["CUDA_VISIBLE_DEVICES"] == '-1':
            self.model.freeze_parameters()
        else:
            self.model.module.freeze_parameters()
        self.init_optim(self.conv_optim_opt, self.model.parameters())

        for epoch in range(self.conv_epoch):
            self.evaluator.reset_metrics()
            logger.info(f'[Conversation epoch {epoch}]')
            logger.info('[Train]')
            for batch in self.train_dataloader.get_conv_data(batch_size=self.conv_batch_size, shuffle=False):
                self.step(batch, stage='conv', mode='train')
            self.evaluator.report(epoch=epoch, mode='train')

            logger.info('[Valid]')
            with torch.no_grad():
                self.evaluator.reset_metrics()
                for batch in self.valid_dataloader.get_conv_data(batch_size=self.conv_batch_size, shuffle=False):
                    self.step(batch, stage='conv', mode='val')
                self.evaluator.report(epoch=epoch, mode='val')

        logger.info('[Test]')
        with torch.no_grad():
            self.evaluator.reset_metrics()
            for batch in self.test_dataloader.get_conv_data(batch_size=self.conv_batch_size, shuffle=False):
                self.step(batch, stage='conv', mode='test')
            self.evaluator.report(mode='test')

    def fit(self):
        self.pretrain()
        self.train_recommender()
        self.train_conversation()

    def interact(self):
        pass
