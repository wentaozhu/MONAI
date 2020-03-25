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

import unittest

import numpy as np
import torch
from torch.utils.data import DataLoader, Dataset

import monai.transforms.compose as transforms
from monai.data.synthetic import create_test_image_2d
from monai.losses.dice import DiceLoss
from monai.networks.nets.unet import UNet
from monai.transforms import (AddChannel, RandRotate90, RandUniformPatch, Rescale)


def run_test(batch_size=64, train_steps=200, device=torch.device("cuda:0")):

    class _TestBatch(Dataset):

        def __init__(self, transforms):
            self.transforms = transforms

        def __getitem__(self, _unused_id):
            im, seg = create_test_image_2d(128, 128, noise_max=1, num_objs=4, num_seg_classes=1)
            seed = np.random.randint(2147483647)
            self.transforms.set_random_state(seed=seed)
            im = self.transforms(im)
            self.transforms.set_random_state(seed=seed)
            seg = self.transforms(seg)
            return im, seg

        def __len__(self):
            return train_steps

    net = UNet(
        dimensions=2,
        in_channels=1,
        out_channels=1,
        channels=(4, 8, 16, 32),
        strides=(2, 2, 2),
        num_res_units=2,
    ).to(device)

    loss = DiceLoss(do_sigmoid=True)
    opt = torch.optim.Adam(net.parameters(), 1e-2)
    train_transforms = transforms.Compose([AddChannel(), Rescale(), RandUniformPatch((96, 96)), RandRotate90()])

    src = DataLoader(_TestBatch(train_transforms), batch_size=batch_size)

    net.train()
    epoch_loss = 0
    step = 0
    for img, seg in src:
        step += 1
        opt.zero_grad()
        output = net(img.to(device))
        step_loss = loss(output, seg.to(device))
        step_loss.backward()
        opt.step()
        epoch_loss += step_loss.item()
    epoch_loss /= step

    return epoch_loss, step


class TestDeterminism(unittest.TestCase):

    def setUp(self):
        np.random.seed(0)
        torch.manual_seed(0)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False
        self.device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu:0')

    def test_training(self):
        loss, step = run_test(device=self.device)
        print('Deterministic loss {} at training step {}'.format(loss, step))
        np.testing.assert_allclose(step, 4)
        np.testing.assert_allclose(loss, 0.5346279, rtol=1e-6)


if __name__ == "__main__":
    unittest.main()
