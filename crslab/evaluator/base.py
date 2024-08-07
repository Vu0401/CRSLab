# @Time   : 2020/11/30
# @Author : Xiaolei Wang
# @Email  : wxl1999@foxmail.com

# UPDATE:
# @Time   : 2020/11/30
# @Author : Xiaolei Wang
# @Email  : wxl1999@foxmail.com

from abc import ABC, abstractmethod


class BaseEvaluator(ABC):
    """Base class for evaluator"""

    def rec_evaluate(self, preds, label):
        # Optional function
        pass

    def gen_evaluate(self, preds, label):
        # Optional function
        pass

    def policy_evaluate(self, preds, label):
        # Optional function
        pass

    @abstractmethod
    def report(self, epoch, mode):
        # Optional function
        pass

    @abstractmethod
    def reset_metrics(self):
        # Optional function
        pass
