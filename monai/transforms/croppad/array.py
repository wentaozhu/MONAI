# Copyright 2020 MONAI Consortium
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
A collection of "vanilla" transforms for crop and pad operations
https://github.com/Project-MONAI/MONAI/wiki/MONAI_Design
"""

from typing import Callable, Optional

import numpy as np
from monai.config.type_definitions import IndexSelection
from monai.data.utils import get_random_patch, get_valid_patch_size
from monai.transforms.compose import Randomizable, Transform
from monai.transforms.utils import generate_spatial_bounding_box
from monai.utils.misc import ensure_tuple, ensure_tuple_rep


class SpatialPad(Transform):
    """
    Performs padding to the data, symmetric for all sides or all on one side for each dimension.
    Uses np.pad so in practice, a mode needs to be provided. See numpy.lib.arraypad.pad
    for additional details.

    Args:
        spatial_size (sequence of int): the spatial size of output data after padding.
        method: pad image symmetric on every side or only pad at the end sides. default is 'symmetric'.
        mode: one of the following string values or a user supplied function: {'constant', 'edge', 'linear_ramp',
            'maximum', 'mean', 'median', 'minimum', 'reflect', 'symmetric', 'wrap', 'empty', <function>}
            for more details, please check: https://docs.scipy.org/doc/numpy/reference/generated/numpy.pad.html
    """

    def __init__(self, spatial_size, method: str = "symmetric", mode: str = "constant"):
        self.spatial_size = ensure_tuple(spatial_size)
        assert method in ("symmetric", "end"), "unsupported padding type."
        self.method = method
        assert isinstance(mode, str), "mode must be str."
        self.mode = mode

    def _determine_data_pad_width(self, data_shape):
        if self.method == "symmetric":
            pad_width = list()
            for i in range(len(self.spatial_size)):
                width = max(self.spatial_size[i] - data_shape[i], 0)
                pad_width.append((width // 2, width - (width // 2)))
            return pad_width
        else:
            return [(0, max(self.spatial_size[i] - data_shape[i], 0)) for i in range(len(self.spatial_size))]

    def __call__(self, img, mode: Optional[str] = None):
        data_pad_width = self._determine_data_pad_width(img.shape[1:])
        all_pad_width = [(0, 0)] + data_pad_width
        if not np.asarray(all_pad_width).any():
            # all zeros, skip padding
            return img
        else:
            img = np.pad(img, all_pad_width, mode=mode or self.mode)
            return img


class BorderPad(Transform):
    """
    Pad the input data by adding specified borders to every dimension.

    Args:
        spatial_border (int or sequence of int): specified size for every spatial border. it can be 3 shapes:
            - single int number, pad all the borders with the same size.
            - length equals the length of image shape, pad every spatial dimension separately.
                for example, image shape(CHW) is [1, 4, 4], spatial_border is [2, 1],
                pad every border of H dim with 2, pad every border of W dim with 1, result shape is [1, 8, 6].
            - length equals 2 x (length of image shape), pad every border of every dimension separately.
                for example, image shape(CHW) is [1, 4, 4], spatial_border is [1, 2, 3, 4], pad top of H dim with 1,
                pad bottom of H dim with 2, pad left of W dim with 3, pad right of W dim with 4.
                the result shape is [1, 7, 11].
        mode: one of the following string values or a user supplied function: {'constant', 'edge', 'linear_ramp',
            'maximum', 'mean', 'median', 'minimum', 'reflect', 'symmetric', 'wrap', 'empty', <function>}
            for more details, please check: https://docs.scipy.org/doc/numpy/reference/generated/numpy.pad.html

    """

    def __init__(self, spatial_border, mode: str = "constant"):
        self.spatial_border = spatial_border
        self.mode = mode

    def __call__(self, img, mode: Optional[str] = None):
        spatial_shape = img.shape[1:]
        spatial_border = ensure_tuple(self.spatial_border)
        for b in spatial_border:
            if b < 0 or not isinstance(b, int):
                raise ValueError("spatial_border must be int number and can not be less than 0.")

        if len(spatial_border) == 1:
            data_pad_width = [(spatial_border[0], spatial_border[0]) for _ in range(len(spatial_shape))]
        elif len(spatial_border) == len(spatial_shape):
            data_pad_width = [(spatial_border[i], spatial_border[i]) for i in range(len(spatial_shape))]
        elif len(spatial_border) == len(spatial_shape) * 2:
            data_pad_width = [(spatial_border[2 * i], spatial_border[2 * i + 1]) for i in range(len(spatial_shape))]
        else:
            raise ValueError("unsupported length of spatial_border definition.")

        return np.pad(img, [(0, 0)] + data_pad_width, mode=mode or self.mode)


class DivisiblePad(Transform):
    """
    Pad the input data, so that the spatial sizes are divisible by `k`.
    """

    def __init__(self, k, mode: str = "constant"):
        """
        Args:
            k (int or sequence of int): the target k for each spatial dimension.
                if `k` is negative or 0, the original size is preserved.
                if `k` is an int, the same `k` be applied to all the input spatial dimensions.
            mode: padding mode for SpatialPad.

        See also :py:class:`monai.transforms.SpatialPad`
        """
        self.k = k
        self.mode = mode

    def __call__(self, img, mode: Optional[str] = None):
        spatial_shape = img.shape[1:]
        k = ensure_tuple_rep(self.k, len(spatial_shape))
        new_size = []
        for k_d, dim in zip(k, spatial_shape):
            new_dim = int(np.ceil(dim / k_d) * k_d) if k_d > 0 else dim
            new_size.append(new_dim)

        spatial_pad = SpatialPad(spatial_size=new_size, method="symmetric", mode=mode or self.mode)
        return spatial_pad(img)


class SpatialCrop(Transform):
    """General purpose cropper to produce sub-volume region of interest (ROI).
    It can support to crop ND spatial (channel-first) data.
    Either a spatial center and size must be provided, or alternatively if center and size
    are not provided, the start and end coordinates of the ROI must be provided.
    The sub-volume must sit the within original image.
    Note: This transform will not work if the crop region is larger than the image itself.
    """

    def __init__(self, roi_center=None, roi_size=None, roi_start=None, roi_end=None):
        """
        Args:
            roi_center (list or tuple): voxel coordinates for center of the crop ROI.
            roi_size (list or tuple): size of the crop ROI.
            roi_start (list or tuple): voxel coordinates for start of the crop ROI.
            roi_end (list or tuple): voxel coordinates for end of the crop ROI.
        """
        if roi_center is not None and roi_size is not None:
            roi_center = np.asarray(roi_center, dtype=np.uint16)
            roi_size = np.asarray(roi_size, dtype=np.uint16)
            self.roi_start = np.subtract(roi_center, np.floor_divide(roi_size, 2))
            self.roi_end = np.add(self.roi_start, roi_size)
        else:
            assert roi_start is not None and roi_end is not None, "roi_start and roi_end must be provided."
            self.roi_start = np.asarray(roi_start, dtype=np.uint16)
            self.roi_end = np.asarray(roi_end, dtype=np.uint16)

        assert np.all(self.roi_start >= 0), "all elements of roi_start must be greater than or equal to 0."
        assert np.all(self.roi_end > 0), "all elements of roi_end must be positive."
        assert np.all(self.roi_end >= self.roi_start), "invalid roi range."

    def __call__(self, img):
        max_end = img.shape[1:]
        sd = min(len(self.roi_start), len(max_end))
        assert np.all(max_end[:sd] >= self.roi_start[:sd]), "roi start out of image space."
        assert np.all(max_end[:sd] >= self.roi_end[:sd]), "roi end out of image space."

        slices = [slice(None)] + [slice(s, e) for s, e in zip(self.roi_start[:sd], self.roi_end[:sd])]
        return img[tuple(slices)]


class CenterSpatialCrop(Transform):
    """
    Crop at the center of image with specified ROI size.

    Args:
        roi_size (list, tuple): the spatial size of the crop region e.g. [224,224,128]
    """

    def __init__(self, roi_size):
        self.roi_size = roi_size

    def __call__(self, img):
        center = [i // 2 for i in img.shape[1:]]
        cropper = SpatialCrop(roi_center=center, roi_size=self.roi_size)
        return cropper(img)


class RandSpatialCrop(Randomizable, Transform):
    """
    Crop image with random size or specific size ROI. It can crop at a random position as center
    or at the image center. And allows to set the minimum size to limit the randomly generated ROI.

    Args:
        roi_size (list, tuple): if `random_size` is True, the spatial size of the minimum crop region.
            if `random_size` is False, specify the expected ROI size to crop. e.g. [224, 224, 128]
        random_center: crop at random position as center or the image center.
        random_size: crop with random size or specific size ROI.
            The actual size is sampled from `randint(roi_size, img_size)`.
    """

    def __init__(self, roi_size, random_center: bool = True, random_size: bool = True):
        self.roi_size = roi_size
        self.random_center = random_center
        self.random_size = random_size
        self._size = None
        self._slices = None

    def randomize(self, img_size):
        self._size = ensure_tuple_rep(self.roi_size, len(img_size))
        if self.random_size:
            self._size = [self.R.randint(low=self._size[i], high=img_size[i] + 1) for i in range(len(img_size))]
        if self.random_center:
            valid_size = get_valid_patch_size(img_size, self._size)
            self._slices = ensure_tuple(slice(None)) + get_random_patch(img_size, valid_size, self.R)

    def __call__(self, img):
        self.randomize(img.shape[1:])
        if self.random_center:
            return img[self._slices]
        else:
            cropper = CenterSpatialCrop(self._size)
            return cropper(img)


class RandSpatialCropSamples(Randomizable, Transform):
    """
    Crop image with random size or specific size ROI to generate a list of N samples.
    It can crop at a random position as center or at the image center. And allows to set
    the minimum size to limit the randomly generated ROI.
    It will return a list of cropped images.

    Args:
        roi_size (list, tuple): if `random_size` is True, the spatial size of the minimum crop region.
            if `random_size` is False, specify the expected ROI size to crop. e.g. [224, 224, 128]
        num_samples: number of samples (crop regions) to take in the returned list.
        random_center: crop at random position as center or the image center.
        random_size: crop with random size or specific size ROI.
            The actual size is sampled from `randint(roi_size, img_size)`.

    """

    def __init__(self, roi_size, num_samples: int, random_center: bool = True, random_size: bool = True):
        if num_samples < 1:
            raise ValueError("number of samples must be greater than 0.")
        self.num_samples = num_samples
        self.cropper = RandSpatialCrop(roi_size, random_center, random_size)

    def randomize(self):
        pass

    def __call__(self, img):
        return [self.cropper(img) for _ in range(self.num_samples)]


class CropForeground(Transform):
    """
    Crop an image using a bounding box. The bounding box is generated by selecting foreground using select_fn
    at channels channel_indexes. margin is added in each spatial dimension of the bounding box.
    The typical usage is to help training and evaluation if the valid part is small in the whole medical image.
    Users can define arbitrary function to select expected foreground from the whole image or specified channels.
    And it can also add margin to every dim of the bounding box of foreground object.
    For example:

    .. code-block:: python

        image = np.array(
            [[[0, 0, 0, 0, 0],
              [0, 1, 2, 1, 0],
              [0, 1, 3, 2, 0],
              [0, 1, 2, 1, 0],
              [0, 0, 0, 0, 0]]])  # 1x5x5, single channel 5x5 image
        cropper = CropForeground(select_fn=lambda x: x > 1, margin=0)
        print(cropper(image))
        [[[2, 1],
          [3, 2],
          [2, 1]]]

    """

    def __init__(
        self, select_fn: Callable = lambda x: x > 0, channel_indexes: Optional[IndexSelection] = None, margin: int = 0
    ):
        """
        Args:
            select_fn: function to select expected foreground, default is to select values > 0.
            channel_indexes: if defined, select foreground only on the specified channels
                of image. if None, select foreground on the whole image.
            margin: add margin to all dims of the bounding box.
        """
        self.select_fn = select_fn
        self.channel_indexes = ensure_tuple(channel_indexes) if channel_indexes is not None else None
        self.margin = margin

    def __call__(self, img):
        box_start, box_end = generate_spatial_bounding_box(img, self.select_fn, self.channel_indexes, self.margin)
        cropper = SpatialCrop(roi_start=box_start, roi_end=box_end)
        return cropper(img)
