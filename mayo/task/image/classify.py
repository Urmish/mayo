import os
import functools

import yaml
import numpy as np
import tensorflow as tf
from tensorflow.contrib import slim

from mayo.log import log
from mayo.util import Percent, memoize_method
from mayo.task.image.base import ImageTaskBase


class Classify(ImageTaskBase):
    _truth_keys = ['class/label']

    def __init__(
            self, session, preprocess,
            background_class, num_classes, shape, moment=None):
        bg = background_class
        self.label_offset = \
            int(bg.get('use', False)) - int(bg.get('has', False))
        self.num_classes = num_classes + self.label_offset
        session.config.dataset.task.num_classes = self.num_classes
        super().__init__(session, preprocess, shape, moment=moment)

    def transform(self, net, data, prediction, truth):
        return data['input'], prediction['output'], truth[0]

    def preprocess(self):
        for images, labels in super().preprocess():
            yield images, labels + self.label_offset

    def _top(self, prediction, truth, num_tops=1):
        return tf.nn.in_top_k(prediction, truth, num_tops)

    def _accuracy(self, prediction, truth, num_tops=1):
        top = self._top(prediction, truth, num_tops)
        return tf.reduce_sum(tf.cast(top, tf.int32)) / top.shape.num_elements()

    @memoize_method
    def _train_setup(self, prediction, truth):
        # formatters
        accuracy_formatter = lambda e: \
            'accuracy: {}'.format(Percent(e.get_mean('accuracy')))
        # register progress update statistics
        accuracy = self._accuracy(prediction, truth)
        self.estimator.register(
            accuracy, 'accuracy', formatter=accuracy_formatter)

    def train(self, net, prediction, truth):
        self._train_setup(prediction, truth)
        truth = slim.one_hot_encoding(truth, self.num_classes)
        return tf.losses.softmax_cross_entropy(
            logits=prediction, onehot_labels=truth)

    @memoize_method
    def _eval_setup(self):
        def metrics(net, prediction, truth):
            top1 = self._top(prediction, truth, 1)
            top5 = self._top(prediction, truth, 5)
            return top1, top5

        top1s, top5s = zip(*self.map(metrics))
        top1s = tf.concat(top1s, axis=0)
        top5s = tf.concat(top5s, axis=0)

        formatted_history = {}

        def formatter(estimator, name):
            history = formatted_history.setdefault(name, [])
            value = estimator.get_value(name)
            history.append(sum(value))
            accuracy = Percent(
                sum(history) / (self.session.batch_size * len(history)))
            return '{}: {}'.format(name, accuracy)

        for tensor, name in ((top1s, 'top1'), (top5s, 'top5')):
            self.estimator.register(
                tensor, name, history='infinite',
                formatter=functools.partial(formatter, name=name))

    def eval(self, net, prediction, truth):
        # set up eval estimators, once and for all predictions and truths
        return self._eval_setup()

    def test(self, names, inputs, predictions):
        results = {}
        for name, image, prediction in zip(names, inputs, predictions):
            name = name.decode()
            label = self.class_names[np.argmax(prediction)]
            log.info('{} labeled as {}.'.format(name, label))
            results[name] = label
        output_dir = self.config.system.search_path.run.outputs[0]
        os.makedirs(output_dir, exist_ok=True)
        filename = os.path.join(output_dir, 'predictions.yaml')
        with open(filename, 'w') as f:
            yaml.dump(results, f)
