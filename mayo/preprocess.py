import collections

import tensorflow as tf

from mayo.log import log
from mayo.util import multi_objects_from_params


class _ImagePreprocess(object):
    def __init__(self, shape, moment, bbox, tid):
        super().__init__()
        self.shape = shape
        self.moment = moment or {}
        self.bbox = bbox
        self.tid = tid

    def distort_bbox(self, i):
        # distort bbox
        bbox_begin, bbox_size, _ = tf.image.sample_distorted_bounding_box(
            tf.shape(i), bounding_boxes=self.bbox,
            min_object_covered=0.1, aspect_ratio_range=[0.75, 1.33],
            area_range=[0.05, 1.0],
            max_attempts=100, use_image_if_no_bounding_boxes=True)
        # distorted image
        i = tf.slice(i, bbox_begin, bbox_size)
        height, width, _ = self.shape
        # we resize to the final preprocessed shape in distort bbox
        # because we can use different methods randomly, which _ensure_shape()
        # cannot do
        i = tf.image.resize_images(i, [height, width], method=(self.tid % 4))
        i.set_shape(self.shape)
        return i

    def distort_color(self, i):
        brightness = lambda i: \
            tf.image.random_brightness(i, max_delta=32.0 / 255.0)
        saturation = lambda i: \
            tf.image.random_saturation(i, lower=0.5, upper=1.5)
        hue = lambda i: \
            tf.image.random_hue(i, max_delta=0.2)
        contrast = lambda i: \
            tf.image.random_contrast(i, lower=0.5, upper=1.5)
        # check if grayscale or color
        channels = i.shape[2]
        if channels == 1:
            # unable to distort saturation and hue for image
            has_color = False
        elif channels == 3:
            has_color = True
        else:
            raise ValueError(
                'Expects the number of channels of an image to be '
                'either 1 or 3.')
        if not has_color:
            order = [brightness, contrast]
        elif self.tid % 2 == 0:
            order = [brightness, saturation, hue, contrast]
        else:
            order = [brightness, contrast, saturation, hue]
        for func in order:
            i = func(i)
        return tf.clip_by_value(i, 0.0, 1.0)

    def central_crop(self, i, fraction=0.875):
        return tf.image.central_crop(i, central_fraction=fraction)

    def random_crop(self, i, height=None, width=None):
        shape = self.shape
        if height and width:
            channels = self.shape[-1]
            shape = [height, width, channels]
        return tf.random_crop(i, shape)

    def crop_or_pad(self, i, height=None, width=None):
        if height is None or width is None:
            height, width, _ = self.shape
        return tf.image.resize_image_with_crop_or_pad(i, height, width)

    def resize(self, i, height=None, width=None, fill=False):
        if height is None or width is None:
            height, width, _ = self.shape
        # fill preserves aspect ratio, resizes the image with minimal cropping
        # and no padding.
        if fill:
            aspect_ratio = tf.constant(width / height)
            ho, wo, _ = tf.unstack(tf.cast(tf.shape(i), tf.float32), 3)
            wo = tf.minimum(tf.round(ho * aspect_ratio), wo)
            ho = tf.minimum(tf.round(wo / aspect_ratio), ho)
            wo = tf.cast(wo, tf.int32)
            ho = tf.cast(ho, tf.int32)
            i = self.crop_or_pad(i, ho, wo)
        i = tf.expand_dims(i, 0)
        i = tf.image.resize_bilinear(i, [height, width], align_corners=False)
        return tf.squeeze(i, [0])

    def random_flip(self, i):
        return tf.image.random_flip_left_right(i)

    def linear_map(self, i, scale=1, shift=0):
        if scale != 1:
            i = tf.multiply(i, scale)
        if shift != 0:
            i = tf.add(i, shift)
        return i

    def subtract_channel_means(self, i):
        means = self.moment.get('mean')
        if not means:
            log.warn(
                'Channel means not supplied, defaulting '
                'to 0.5 for each channel.')
            means = [0.5] * i.shape[2]
        shape = [1, 1, len(means)]
        means = tf.constant(means, shape=shape, name='image_means')
        return i - means

    def normalize_channels(self, i):
        i = self.subtract_channel_means(i)
        stds = self.moment.get('std')
        if not stds:
            log.warn(
                'Channel std value not supplied, defaulting '
                'to 1.0 for each channel.')
            return i
        shape = [1, 1, len(stds)]
        stds = tf.constant(stds, shape=shape, name='image_stds')
        return i / stds

    def subtract_image_mean(self, i):
        return i - tf.reduce_mean(i)

    def standardize_image(self, i):
        return tf.image.per_image_standardization(i)

    def permute_channels(self, i, order):
        channels = len(order)
        channel_splits = tf.split(i, channels, axis=-1)
        permuted_splits = [channel_splits[o] for o in order]
        return tf.concat(permuted_splits, -1)

    def _ensure_shape(self, i):
        # ensure image is the correct shape
        ph, pw, pc = i.shape.as_list()
        h, w, c = self.shape
        if pc != c:
            # convert channels
            if pc == 3 and c == 1:
                i = tf.image.rgb_to_grayscale(i)
            elif pc == 1:
                # duplicate image channel
                i = tf.concat([i] * c, axis=-1)
            else:
                raise ValueError(
                    'We do not know how to convert an image with {} channels '
                    'into one with {} channels.'.format(pc, c))
        if ph == h or pw == w:
            log.debug(
                'Size of image {} x {} is equal to the expected preprocessed '
                'shape.'.format(h, w))
            return i
        log.debug(
            'Ensuring size of image is as expected by our inputs {} x {} by '
            'resizing it...'.format(h, w))
        # rescale image
        return self.resize(i, h, w, fill=True)

    def preprocess(self, image, actions):
        with tf.name_scope(values=[image], name='preprocess_image'):
            for func, params in multi_objects_from_params(actions, self):
                log.debug(
                    'Preprocessing using {!r} with params {}'
                    .format(func.__name__, params))
                image = func(image, **params)
        return self._ensure_shape(image)


class Preprocess(object):
    images_per_shard = 1024
    queue_memory_factor = 16
    num_readers = 4

    def __init__(self, session, mode, concurrent, config):
        super().__init__()
        self.session = session
        if mode not in ['train', 'validate']:
            raise ValueError(
                'Unrecognized preprocessing mode {!r}'.format(mode))
        self.mode = mode
        self.concurrent = concurrent
        self.config = config
        self._coord = tf.train.Coordinator()
        self.is_started = False

    def __del__(self):
        self.stop()

    def start(self):
        if self.is_started:
            return
        # queue runners
        self._threads = tf.train.start_queue_runners(
            sess=self.session, coord=self._coord)
        self.is_started = True

    def stop(self):
        if not self.is_started:
            return
        self._coord.request_stop()
        self._coord.join(self._threads, stop_grace_period_secs=10)
        self.is_started = False

    @staticmethod
    def _decode_jpeg(buffer, channels):
        with tf.name_scope(values=[buffer], name='decode_jpeg'):
            i = tf.image.decode_jpeg(buffer, channels=channels)
            return tf.image.convert_image_dtype(i, dtype=tf.float32)

    def _preprocess(self, buffer, bbox, tid):
        channels = self.config.image_shape()[-1]
        image = self._decode_jpeg(buffer, channels)
        shape = self.config.image_shape()
        moment = self.config.dataset.get('moment')
        image_preprocess = _ImagePreprocess(shape, moment, bbox, tid)
        actions_map = self.config.dataset.preprocess
        mode_actions = actions_map[self.mode] or []
        if not isinstance(mode_actions, collections.Sequence):
            mode_actions = [mode_actions]
        final_actions = actions_map['final'] or []
        if not isinstance(final_actions, collections.Sequence):
            final_actions = [final_actions]
        return image_preprocess.preprocess(image, mode_actions + final_actions)

    def _filename_queue(self):
        """
        Queue for file names to read from
        """
        files = self.config.data_files(self.mode)
        if self.mode == 'train':
            shuffle = True
            capacity = 16
        else:
            shuffle = False
            capacity = 1
        return tf.train.string_input_producer(
            files, shuffle=shuffle, capacity=capacity)

    def _queue(self):
        """
        Queue for serialized image data
        """
        min_images_in_queue = self.images_per_shard * self.queue_memory_factor
        batch_size = self.config.system.batch_size
        if self.mode == 'train':
            # shuffling
            return tf.RandomShuffleQueue(
                min_images_in_queue + 3 * batch_size,
                min_after_dequeue=min_images_in_queue, dtypes=[tf.string])
        return tf.FIFOQueue(
            self.images_per_shard + 3 * batch_size, dtypes=[tf.string])

    def _reader(self):
        """
        File reader
        """
        return tf.TFRecordReader()

    def _serialized_inputs(self):
        """
        Reads data to populate the queue, and pops serialized data from queue
        """
        filename_queue = self._filename_queue()
        is_training = self.mode == 'train'
        if is_training and self.config.system.preprocess.num_readers > 1:
            queue = self._queue()
            enqueue_ops = []
            for _ in range(self.num_readers):
                _, value = self._reader().read(filename_queue)
                enqueue_ops.append(queue.enqueue([value]))
            qr = tf.train.queue_runner
            qr.add_queue_runner(qr.QueueRunner(queue, enqueue_ops))
            serialized = queue.dequeue()
        else:
            _, serialized = self._reader().read(filename_queue)
        return serialized

    @staticmethod
    def _parse_proto(proto):
        # dense features
        string = tf.FixedLenFeature([], dtype=tf.string, default_value='')
        integer = tf.FixedLenFeature([1], dtype=tf.int64, default_value=-1)
        feature_map = {
            'image/encoded': string,
            'image/class/label': integer,
            'image/class/text': string,
        }
        float32 = tf.VarLenFeature(dtype=tf.float32)
        # bounding boxes
        bbox_keys = [
            'image/object/bbox/xmin', 'image/object/bbox/ymin',
            'image/object/bbox/xmax', 'image/object/bbox/ymax']
        for k in bbox_keys:
            feature_map[k] = float32

        # parsing
        features = tf.parse_single_example(proto, feature_map)
        label = tf.cast(features['image/class/label'], dtype=tf.int32)

        # bbox handling
        xmin, ymin, xmax, ymax = (
            tf.expand_dims(features[k].values, 0) for k in bbox_keys)
        # tensorflow imposes an ordering of (y, x) just to make life difficult
        bbox = tf.concat(axis=0, values=[ymin, xmin, ymax, xmax])
        # force the variable number of bounding boxes into the shape
        # [1, num_boxes, coords]
        bbox = tf.expand_dims(bbox, 0)
        bbox = tf.transpose(bbox, [0, 2, 1])

        encoded = features['image/encoded']
        text = features['image/class/text']
        return encoded, label, bbox, text

    def _unserialize(self, serialized):
        num_threads = self.config.system.preprocess.num_threads
        if self.concurrent:
            if num_threads % 4:
                raise ValueError(
                    'Expect number of threads to be a multiple of 4.')
        else:
            num_threads = 1
        images_labels = []
        for tid in range(num_threads):
            log.debug('Preprocessing thread #{}'.format(tid))
            buffer, label, bbox, _ = self._parse_proto(serialized)
            image = self._preprocess(buffer, bbox, tid)
            offset = self.config.label_offset()
            log.debug('Incrementing label by offset {}'.format(offset))
            label += offset
            images_labels.append((image, label))
        batch_size = self.config.system.batch_size
        capacity = 2 * num_threads * batch_size
        images, labels = tf.train.batch_join(
            images_labels, batch_size=batch_size, capacity=capacity)
        images = tf.cast(images, tf.float32)
        shape = (batch_size, ) + self.config.image_shape()
        images = tf.reshape(images, shape=shape)
        return images, tf.reshape(labels, [batch_size])

    def preprocess(self, num_gpus):
        with tf.name_scope('batch_processing'):
            serialized = self._serialized_inputs()
            images, labels = self._unserialize(serialized)
            self.start()
            return zip(tf.split(images, num_gpus), tf.split(labels, num_gpus))
