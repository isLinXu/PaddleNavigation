import numpy as np
import cv2
import pickle
import math
import random

import os

from PIL import Image

try:
    import cPickle as pickle
    from cStringIO import StringIO
    import av
    import decord as de
    import SimpleITK as sitk
except ImportError:
    import pickle
    from io import BytesIO

from collections.abc import Sequence


class VideoDecoder(object):
    """
    Decode mp4 file to frames.
    Args:
        filepath: the file path of mp4 file
    """
    def __init__(self,
                 backend='cv2',
                 mode='train',
                 sampling_rate=32,
                 num_seg=8,
                 num_clips=1,
                 target_fps=30):

        self.backend = backend
        # params below only for TimeSformer
        self.mode = mode
        self.sampling_rate = sampling_rate
        self.num_seg = num_seg
        self.num_clips = num_clips
        self.target_fps = target_fps

    def __call__(self, results):
        """
        Perform mp4 decode operations.
        return:
            List where each item is a numpy array after decoder.
        """
        file_path = results['filename']
        results['format'] = 'video'
        results['backend'] = self.backend

        if self.backend == 'cv2':
            cap = cv2.VideoCapture(file_path)
            videolen = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            sampledFrames = []
            for i in range(videolen):
                ret, frame = cap.read()
                # maybe first frame is empty
                if ret == False:
                    continue
                img = frame[:, :, ::-1]
                sampledFrames.append(img)
            results['frames'] = sampledFrames
            results['frames_len'] = len(sampledFrames)

        elif self.backend == 'decord':
            container = de.VideoReader(file_path)
            frames_len = len(container)
            results['frames'] = container
            results['frames_len'] = frames_len

        elif self.backend == 'pyav':  # for TimeSformer
            if self.mode in ["train", "valid"]:
                clip_idx = -1
            elif self.mode in ["test"]:
                clip_idx = 0
            else:
                raise NotImplementedError

            container = av.open(file_path)

            num_clips = 1  # always be 1

            # decode process
            fps = float(container.streams.video[0].average_rate)

            frames_length = container.streams.video[0].frames
            duration = container.streams.video[0].duration

            if duration is None:
                # If failed to fetch the decoding information, decode the entire video.
                decode_all_video = True
                video_start_pts, video_end_pts = 0, math.inf
            else:
                decode_all_video = False
                start_idx, end_idx = get_start_end_idx(
                    frames_length,
                    self.sampling_rate * self.num_seg / self.target_fps * fps,
                    clip_idx, num_clips)
                timebase = duration / frames_length
                video_start_pts = int(start_idx * timebase)
                video_end_pts = int(end_idx * timebase)

            frames = None
            # If video stream was found, fetch video frames from the video.
            if container.streams.video:
                margin = 1024
                seek_offset = max(video_start_pts - margin, 0)

                container.seek(seek_offset,
                               any_frame=False,
                               backward=True,
                               stream=container.streams.video[0])
                tmp_frames = {}
                buffer_count = 0
                max_pts = 0
                for frame in container.decode(**{"video": 0}):
                    max_pts = max(max_pts, frame.pts)
                    if frame.pts < video_start_pts:
                        continue
                    if frame.pts <= video_end_pts:
                        tmp_frames[frame.pts] = frame
                    else:
                        buffer_count += 1
                        tmp_frames[frame.pts] = frame
                        if buffer_count >= 0:
                            break
                video_frames = [tmp_frames[pts] for pts in sorted(tmp_frames)]

                container.close()

                frames = [frame.to_rgb().to_ndarray() for frame in video_frames]
                clip_sz = self.sampling_rate * self.num_seg / self.target_fps * fps

                start_idx, end_idx = get_start_end_idx(
                    len(frames),  # frame_len
                    clip_sz,
                    clip_idx if decode_all_video else
                    0,  # If decode all video, -1 in train and valid, 0 in test;
                    # else, always 0 in train, valid and test, as we has selected clip size frames when decode.
                    1)
                results['frames'] = frames
                results['frames_len'] = len(frames)
                results['start_idx'] = start_idx
                results['end_idx'] = end_idx
        else:
            raise NotImplementedError
        return results


class Sampler(object):
    """
    Sample frames id.
    NOTE: Use PIL to read image here, has diff with CV2
    Args:
        num_seg(int): number of segments.
        seg_len(int): number of sampled frames in each segment.
        valid_mode(bool): True or False.
        select_left: Whether to select the frame to the left in the middle when the sampling interval is even in the test mode.
    Returns:
        frames_idx: the index of sampled #frames.
    """
    def __init__(self,
                 num_seg,
                 seg_len,
                 frame_interval=None,
                 valid_mode=False,
                 select_left=False,
                 dense_sample=False,
                 linspace_sample=False,
                 use_pil=True):
        self.num_seg = num_seg
        self.seg_len = seg_len
        self.frame_interval = frame_interval
        self.valid_mode = valid_mode
        self.select_left = select_left
        self.dense_sample = dense_sample
        self.linspace_sample = linspace_sample
        self.use_pil = use_pil

    def _get(self, frames_idx, results):
        data_format = results['format']

        if data_format == "frame":
            frame_dir = results['frame_dir']
            imgs = []
            for idx in frames_idx:
                img = Image.open(
                    os.path.join(frame_dir,
                                 results['suffix'].format(idx))).convert('RGB')
                imgs.append(img)

        elif data_format == "MRI":
            frame_dir = results['frame_dir']
            imgs = []
            MRI = sitk.GetArrayFromImage(sitk.ReadImage(frame_dir))
            for idx in frames_idx:
                item = MRI[idx]
                item = cv2.resize(item, (224, 224))
                imgs.append(item)

        elif data_format == "video":
            if results['backend'] == 'cv2':
                frames = np.array(results['frames'])
                imgs = []
                for idx in frames_idx:
                    imgbuf = frames[idx]
                    img = Image.fromarray(imgbuf, mode='RGB')
                    imgs.append(img)
            elif results['backend'] == 'decord':
                container = results['frames']
                if self.use_pil:
                    frames_select = container.get_batch(frames_idx)
                    # dearray_to_img
                    np_frames = frames_select.asnumpy()
                    imgs = []
                    for i in range(np_frames.shape[0]):
                        imgbuf = np_frames[i]
                        imgs.append(Image.fromarray(imgbuf, mode='RGB'))
                else:
                    if frames_idx.ndim != 1:
                        frames_idx = np.squeeze(frames_idx)
                    frame_dict = {
                        idx: container[idx].asnumpy()
                        for idx in np.unique(frames_idx)
                    }
                    imgs = [frame_dict[idx] for idx in frames_idx]
            elif results['backend'] == 'pyav':
                imgs = []
                frames = np.array(results['frames'])
                for idx in frames_idx:
                    imgbuf = frames[idx]
                    imgs.append(imgbuf)
                imgs = np.stack(imgs)  # thwc
            else:
                raise NotImplementedError
        else:
            raise NotImplementedError
        results['imgs'] = imgs
        return results

    def _get_train_clips(self, num_frames):
        ori_seg_len = self.seg_len * self.frame_interval
        avg_interval = (num_frames - ori_seg_len + 1) // self.num_seg

        if avg_interval > 0:
            base_offsets = np.arange(self.num_seg) * avg_interval
            clip_offsets = base_offsets + np.random.randint(avg_interval,
                                                            size=self.num_seg)
        elif num_frames > max(self.num_seg, ori_seg_len):
            clip_offsets = np.sort(
                np.random.randint(num_frames - ori_seg_len + 1,
                                  size=self.num_seg))
        elif avg_interval == 0:
            ratio = (num_frames - ori_seg_len + 1.0) / self.num_seg
            clip_offsets = np.around(np.arange(self.num_seg) * ratio)
        else:
            clip_offsets = np.zeros((self.num_seg, ), dtype=np.int)
        return clip_offsets

    def _get_test_clips(self, num_frames):
        ori_seg_len = self.seg_len * self.frame_interval
        avg_interval = (num_frames - ori_seg_len + 1) / float(self.num_seg)
        if num_frames > ori_seg_len - 1:
            base_offsets = np.arange(self.num_seg) * avg_interval
            clip_offsets = (base_offsets + avg_interval / 2.0).astype(np.int)
        else:
            clip_offsets = np.zeros((self.num_seg, ), dtype=np.int)
        return clip_offsets

    def __call__(self, results):
        """
        Args:
            frames_len: length of frames.
        return:
            sampling id.
        """
        frames_len = int(results['frames_len'])
        frames_idx = []
        if self.frame_interval is not None:
            assert isinstance(self.frame_interval, int)
            if not self.valid_mode:
                offsets = self._get_train_clips(frames_len)
            else:
                offsets = self._get_test_clips(frames_len)

            offsets = offsets[:, None] + np.arange(
                self.seg_len)[None, :] * self.frame_interval
            offsets = np.concatenate(offsets)

            offsets = offsets.reshape((-1, self.seg_len))
            offsets = np.mod(offsets, frames_len)
            offsets = np.concatenate(offsets)

            if results['format'] == 'video':
                frames_idx = offsets
            elif results['format'] == 'frame':
                frames_idx = list(offsets + 1)
            else:
                raise NotImplementedError

            return self._get(frames_idx, results)

        if self.linspace_sample:
            if 'start_idx' in results and 'end_idx' in results:
                offsets = np.linspace(results['start_idx'], results['end_idx'],
                                      self.num_seg)
            else:
                offsets = np.linspace(0, frames_len - 1, self.num_seg)
            offsets = np.clip(offsets, 0, frames_len - 1).astype(np.int64)
            if results['format'] == 'video':
                frames_idx = list(offsets)
                frames_idx = [x % frames_len for x in frames_idx]
            elif results['format'] == 'frame':
                frames_idx = list(offsets + 1)

            elif results['format'] == 'MRI':
                frames_idx = list(offsets)

            else:
                raise NotImplementedError
            return self._get(frames_idx, results)

        average_dur = int(frames_len / self.num_seg)
        if not self.select_left:
            if self.dense_sample:  # For ppTSM
                if not self.valid_mode:  # train
                    sample_pos = max(1, 1 + frames_len - 64)
                    t_stride = 64 // self.num_seg
                    start_idx = 0 if sample_pos == 1 else np.random.randint(
                        0, sample_pos - 1)
                    offsets = [(idx * t_stride + start_idx) % frames_len + 1
                               for idx in range(self.num_seg)]
                    frames_idx = offsets
                else:
                    sample_pos = max(1, 1 + frames_len - 64)
                    t_stride = 64 // self.num_seg
                    start_list = np.linspace(0,
                                             sample_pos - 1,
                                             num=10,
                                             dtype=int)
                    offsets = []
                    for start_idx in start_list.tolist():
                        offsets += [
                            (idx * t_stride + start_idx) % frames_len + 1
                            for idx in range(self.num_seg)
                        ]
                    frames_idx = offsets
            else:
                for i in range(self.num_seg):
                    idx = 0
                    if not self.valid_mode:
                        if average_dur >= self.seg_len:
                            idx = random.randint(0, average_dur - self.seg_len)
                            idx += i * average_dur
                        elif average_dur >= 1:
                            idx += i * average_dur
                        else:
                            idx = i
                    else:
                        if average_dur >= self.seg_len:
                            idx = (average_dur - 1) // 2
                            idx += i * average_dur
                        elif average_dur >= 1:
                            idx += i * average_dur
                        else:
                            idx = i
                    for jj in range(idx, idx + self.seg_len):
                        if results['format'] == 'video':
                            frames_idx.append(int(jj % frames_len))
                        elif results['format'] == 'frame':
                            frames_idx.append(jj + 1)

                        elif results['format'] == 'MRI':
                            frames_idx.append(jj)
                        else:
                            raise NotImplementedError
            return self._get(frames_idx, results)

        else:  # for TSM
            if not self.valid_mode:
                if average_dur > 0:
                    offsets = np.multiply(list(range(self.num_seg)),
                                          average_dur) + np.random.randint(
                                              average_dur, size=self.num_seg)
                elif frames_len > self.num_seg:
                    offsets = np.sort(
                        np.random.randint(frames_len, size=self.num_seg))
                else:
                    offsets = np.zeros(shape=(self.num_seg, ))
            else:
                if frames_len > self.num_seg:
                    average_dur_float = frames_len / self.num_seg
                    offsets = np.array([
                        int(average_dur_float / 2.0 + average_dur_float * x)
                        for x in range(self.num_seg)
                    ])
                else:
                    offsets = np.zeros(shape=(self.num_seg, ))

            if results['format'] == 'video':
                frames_idx = list(offsets)
                frames_idx = [x % frames_len for x in frames_idx]
            elif results['format'] == 'frame':
                frames_idx = list(offsets + 1)

            elif results['format'] == 'MRI':
                frames_idx = list(offsets)

            else:
                raise NotImplementedError

            return self._get(frames_idx, results)

class CenterCrop(object):
    """
    Center crop images.
    Args:
        target_size(int): Center crop a square with the target_size from an image.
        do_round(bool): Whether to round up the coordinates of the upper left corner of the cropping area. default: True
    """
    def __init__(self, target_size, do_round=True, backend='pillow'):
        self.target_size = target_size
        self.do_round = do_round
        self.backend = backend

    def __call__(self, results):
        """
        Performs Center crop operations.
        Args:
            imgs: List where each item is a PIL.Image.
            For example, [PIL.Image0, PIL.Image1, PIL.Image2, ...]
        return:
            ccrop_imgs: List where each item is a PIL.Image after Center crop.
        """
        import paddle
        imgs = results['imgs']
        ccrop_imgs = []
        th, tw = self.target_size, self.target_size
        if isinstance(imgs, paddle.Tensor):
            h, w = imgs.shape[-2:]
            x1 = int(round((w - tw) / 2.0)) if self.do_round else (w - tw) // 2
            y1 = int(round((h - th) / 2.0)) if self.do_round else (h - th) // 2
            ccrop_imgs = imgs[:, :, y1:y1 + th, x1:x1 + tw]
        else:
            for img in imgs:
                if self.backend == 'pillow':
                    w, h = img.size
                elif self.backend == 'cv2':
                    h, w, _ = img.shape
                else:
                    raise NotImplementedError
                assert (w >= self.target_size) and (h >= self.target_size), \
                    "image width({}) and height({}) should be larger than crop size".format(
                        w, h, self.target_size)
                x1 = int(round(
                    (w - tw) / 2.0)) if self.do_round else (w - tw) // 2
                y1 = int(round(
                    (h - th) / 2.0)) if self.do_round else (h - th) // 2
                if self.backend == 'cv2':
                    ccrop_imgs.append(img[y1:y1 + th, x1:x1 + tw])
                elif self.backend == 'pillow':
                    ccrop_imgs.append(img.crop((x1, y1, x1 + tw, y1 + th)))
        results['imgs'] = ccrop_imgs
        return results

class Scale(object):
    """
    Scale images.
    Args:
        short_size(float | int): Short size of an image will be scaled to the short_size.
        fixed_ratio(bool): Set whether to zoom according to a fixed ratio. default: True
        do_round(bool): Whether to round up when calculating the zoom ratio. default: False
        backend(str): Choose pillow or cv2 as the graphics processing backend. default: 'pillow'
    """
    def __init__(self,
                 short_size,
                 fixed_ratio=True,
                 keep_ratio=None,
                 do_round=False,
                 backend='pillow'):
        self.short_size = short_size
        assert (fixed_ratio and not keep_ratio) or (not fixed_ratio), \
            f"fixed_ratio and keep_ratio cannot be true at the same time"
        self.fixed_ratio = fixed_ratio
        self.keep_ratio = keep_ratio
        self.do_round = do_round

        assert backend in [
            'pillow', 'cv2'
        ], f"Scale's backend must be pillow or cv2, but get {backend}"
        self.backend = backend

    def __call__(self, results):
        """
        Performs resize operations.
        Args:
            imgs (Sequence[PIL.Image]): List where each item is a PIL.Image.
            For example, [PIL.Image0, PIL.Image1, PIL.Image2, ...]
        return:
            resized_imgs: List where each item is a PIL.Image after scaling.
        """
        imgs = results['imgs']
        resized_imgs = []
        for i in range(len(imgs)):
            img = imgs[i]
            if isinstance(img, np.ndarray):
                h, w, _ = img.shape
            elif isinstance(img, Image.Image):
                w, h = img.size
            else:
                raise NotImplementedError

            if w <= h:
                ow = self.short_size
                if self.fixed_ratio:
                    oh = int(self.short_size * 4.0 / 3.0)
                elif not self.keep_ratio:  # no
                    oh = self.short_size
                else:
                    scale_factor = self.short_size / w
                    oh = int(h * float(scale_factor) +
                             0.5) if self.do_round else int(h *
                                                            self.short_size / w)
                    ow = int(w * float(scale_factor) +
                             0.5) if self.do_round else int(w *
                                                            self.short_size / h)
            else:
                oh = self.short_size
                if self.fixed_ratio:
                    ow = int(self.short_size * 4.0 / 3.0)
                elif not self.keep_ratio:  # no
                    ow = self.short_size
                else:
                    scale_factor = self.short_size / h
                    oh = int(h * float(scale_factor) +
                             0.5) if self.do_round else int(h *
                                                            self.short_size / w)
                    ow = int(w * float(scale_factor) +
                             0.5) if self.do_round else int(w *
                                                            self.short_size / h)
            if self.backend == 'pillow':
                resized_imgs.append(img.resize((ow, oh), Image.BILINEAR))
            elif self.backend == 'cv2' and (self.keep_ratio is not None):
                resized_imgs.append(
                    cv2.resize(img, (ow, oh), interpolation=cv2.INTER_LINEAR))
            else:
                resized_imgs.append(
                    Image.fromarray(
                        cv2.resize(np.asarray(img), (ow, oh),
                                   interpolation=cv2.INTER_LINEAR)))
        results['imgs'] = resized_imgs
        return results

class Image2Array(object):
    """
    transfer PIL.Image to Numpy array and transpose dimensions from 'dhwc' to 'dchw'.
    Args:
        transpose: whether to transpose or not, default True, False for slowfast.
    """
    def __init__(self, transpose=True, data_format='tchw'):
        assert data_format in [
            'tchw', 'cthw'
        ], f"Target format must in ['tchw', 'cthw'], but got {data_format}"
        self.transpose = transpose
        self.data_format = data_format

    def __call__(self, results):
        """
        Performs Image to NumpyArray operations.
        Args:
            imgs: List where each item is a PIL.Image.
            For example, [PIL.Image0, PIL.Image1, PIL.Image2, ...]
        return:
            np_imgs: Numpy array.
        """
        imgs = results['imgs']
        if 'backend' in results and results[
                'backend'] == 'pyav':  # [T,H,W,C] in [0, 1]
            if self.transpose:
                if self.data_format == 'tchw':
                    t_imgs = imgs.transpose((0, 3, 1, 2))  # tchw
                else:
                    t_imgs = imgs.transpose((3, 0, 1, 2))  # cthw
            results['imgs'] = t_imgs
        else:
            t_imgs = np.stack(imgs).astype('float32')
            if self.transpose:
                if self.data_format == 'tchw':
                    t_imgs = t_imgs.transpose(0, 3, 1, 2)  # tchw
                else:
                    t_imgs = t_imgs.transpose(3, 0, 1, 2)  # cthw
            results['imgs'] = t_imgs
        return results

class Normalization(object):
    """
    Normalization.
    Args:
        mean(Sequence[float]): mean values of different channels.
        std(Sequence[float]): std values of different channels.
        tensor_shape(list): size of mean, default [3,1,1]. For slowfast, [1,1,1,3]
    """
    def __init__(self, mean, std, tensor_shape=[3, 1, 1], inplace=False):
        if not isinstance(mean, Sequence):
            raise TypeError(
                f'Mean must be list, tuple or np.ndarray, but got {type(mean)}')
        if not isinstance(std, Sequence):
            raise TypeError(
                f'Std must be list, tuple or np.ndarray, but got {type(std)}')

        self.inplace = inplace
        if not inplace:
            self.mean = np.array(mean).reshape(tensor_shape).astype(np.float32)
            self.std = np.array(std).reshape(tensor_shape).astype(np.float32)
        else:
            self.mean = np.array(mean, dtype=np.float32)
            self.std = np.array(std, dtype=np.float32)

    def __call__(self, results):
        """
        Performs normalization operations.
        Args:
            imgs: Numpy array.
        return:
            np_imgs: Numpy array after normalization.
        """
        import paddle
        if self.inplace:
            n = len(results['imgs'])
            h, w, c = results['imgs'][0].shape
            norm_imgs = np.empty((n, h, w, c), dtype=np.float32)
            for i, img in enumerate(results['imgs']):
                norm_imgs[i] = img

            for img in norm_imgs:  # [n,h,w,c]
                mean = np.float64(self.mean.reshape(1, -1))  # [1, 3]
                stdinv = 1 / np.float64(self.std.reshape(1, -1))  # [1, 3]
                cv2.subtract(img, mean, img)
                cv2.multiply(img, stdinv, img)
        else:
            imgs = results['imgs']
            norm_imgs = imgs / 255.0
            norm_imgs -= self.mean
            norm_imgs /= self.std
            if 'backend' in results and results['backend'] == 'pyav':
                norm_imgs = paddle.to_tensor(norm_imgs, dtype=paddle.float32)
        results['imgs'] = norm_imgs
        return results

class TenCrop:
    """
    Crop out 5 regions (4 corner points + 1 center point) from the picture,
    and then flip the cropping result to get 10 cropped images, which can make the prediction result more robust.
    Args:
        target_size(int | tuple[int]): (w, h) of target size for crop.
    """
    def __init__(self, target_size):
        self.target_size = (target_size, target_size)

    def __call__(self, results):
        imgs = results['imgs']
        img_w, img_h = imgs[0].size
        crop_w, crop_h = self.target_size
        w_step = (img_w - crop_w) // 4
        h_step = (img_h - crop_h) // 4
        offsets = [
            (0, 0),
            (4 * w_step, 0),
            (0, 4 * h_step),
            (4 * w_step, 4 * h_step),
            (2 * w_step, 2 * h_step),
        ]
        img_crops = list()
        for x_offset, y_offset in offsets:
            crop = [
                img.crop(
                    (x_offset, y_offset, x_offset + crop_w, y_offset + crop_h))
                for img in imgs
            ]
            crop_fliped = [
                timg.transpose(Image.FLIP_LEFT_RIGHT) for timg in crop
            ]
            img_crops.extend(crop)
            img_crops.extend(crop_fliped)

        results['imgs'] = img_crops
        return results
