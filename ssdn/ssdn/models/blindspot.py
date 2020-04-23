import torch
import ssdn
import torch.nn as nn


# TODO: Should we actually be reusing weights for block2, this is not done on
# other implementations of UNET
# TODO: Should we be adding Dropout, this is used on some unet implementations.
# TODO: What should the linear output function be?
# TODO: Add shift to downsample for blindspot (max pool)

class ShiftConv2d(nn.Conv2d):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.shift_size = self.kernel_size[0] // 2
        self.pad = nn.ZeroPad2d((0, 0, self.shift_size, 0))

    def forward(self, x):
        x = self.pad(x)
        x = super().forward(x)
        if self.shift_size:
            x = x[:, :, : -self.shift_size, :]
        return x


class Crop2d(nn.Module):
    def __init__(self, crop):
        super().__init__()
        self.crop = crop
        # Assume BCHW
        assert len(crop) == 4

    def forward(self, x):
        (left, right, top, bottom) = self.crop
        x0, x1 = left, x.shape[-1] - right
        y0, y1 = top, x.shape[-2] - bottom
        return x[:, :, y0:y1, x0:x1]


class NoiseNetwork(nn.Module):
    """Custom U-Net architecture for Noise2Noise (see Appendix, Table 2).
        Added Blindspot features to the Noise2Noise implementation by @joeylitalien
    """

    @property
    def blindspot(self) -> bool:
        return self._blindspot

    def __init__(self, in_channels=3, out_channels=3, blindspot=False):
        """Initializes U-Net."""

        super(NoiseNetwork, self).__init__()
        self._blindspot = blindspot

        if self.blindspot:
            self.Conv2d = ShiftConv2d
        else:
            self.Conv2d = nn.Conv2d

        # Layers: enc_conv0, enc_conv1, pool1
        self._block1 = nn.Sequential(
            self.Conv2d(in_channels, 48, 3, stride=1, padding=1),
            nn.LeakyReLU(negative_slope=0.1, inplace=True),
            self.Conv2d(48, 48, 3, padding=1),
            nn.LeakyReLU(negative_slope=0.1, inplace=True),
            nn.MaxPool2d(2),
        )

        # Layers: enc_conv(i), pool(i); i=2..5
        self._block2 = nn.Sequential(
            self.Conv2d(48, 48, 3, stride=1, padding=1),
            nn.LeakyReLU(negative_slope=0.1, inplace=True),
            nn.MaxPool2d(2),
        )

        # Layers: enc_conv6, upsample5
        self._block3 = nn.Sequential(
            self.Conv2d(48, 48, 3, stride=1, padding=1),
            nn.LeakyReLU(negative_slope=0.1, inplace=True),
            nn.ConvTranspose2d(48, 48, 3, stride=2, padding=1, output_padding=1),
        )

        # Layers: dec_conv5a, dec_conv5b, upsample4
        self._block4 = nn.Sequential(
            self.Conv2d(96, 96, 3, stride=1, padding=1),
            nn.LeakyReLU(negative_slope=0.1, inplace=True),
            self.Conv2d(96, 96, 3, stride=1, padding=1),
            nn.LeakyReLU(negative_slope=0.1, inplace=True),
            nn.ConvTranspose2d(96, 96, 3, stride=2, padding=1, output_padding=1),
        )

        # Layers: dec_deconv(i)a, dec_deconv(i)b, upsample(i-1); i=4..2
        self._block5 = nn.Sequential(
            self.Conv2d(144, 96, 3, stride=1, padding=1),
            nn.LeakyReLU(negative_slope=0.1, inplace=True),
            self.Conv2d(96, 96, 3, stride=1, padding=1),
            nn.LeakyReLU(negative_slope=0.1, inplace=True),
            nn.ConvTranspose2d(96, 96, 3, stride=2, padding=1, output_padding=1),
        )

        # Layers: dec_conv1a, dec_conv1b, dec_conv1c,
        self._block6 = nn.Sequential(
            self.Conv2d(96 + in_channels, 96, 3, stride=1, padding=1),
            nn.LeakyReLU(negative_slope=0.1, inplace=True),
            self.Conv2d(96, 96, 3, stride=1, padding=1),
            nn.LeakyReLU(negative_slope=0.1, inplace=True),
        )

        if self.blindspot:
            self.shift = nn.Sequential(nn.ZeroPad2d((0, 0, 1, 0)), Crop2d((0, 0, 0, 1)))
            nin_a_io = 384
        else:
            nin_a_io = 96

        # nin_a,b,c, linear_act
        self._block7 = nn.Sequential(
            self.Conv2d(nin_a_io, nin_a_io, 1),
            nn.LeakyReLU(negative_slope=0.1, inplace=True),
            self.Conv2d(nin_a_io, 96, 1),
            nn.LeakyReLU(negative_slope=0.1, inplace=True),
            self.Conv2d(96, out_channels, 1),
        )

        # Initialize weights
        self._init_weights()

    def _init_weights(self):
        """Initializes weights using He et al. (2015)."""

        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight.data)
                m.bias.data.zero_()

    def forward(self, x):
        if self.blindspot:
            rotated = [ssdn.utils.rotate(x, rot) for rot in (0, 90, 180, 270)]
            x = torch.cat((rotated), dim=0)

        # Encoder
        pool1 = self._block1(x)
        pool2 = self._block2(pool1)
        pool3 = self._block2(pool2)
        pool4 = self._block2(pool3)
        pool5 = self._block2(pool4)

        # Decoder
        upsample5 = self._block3(pool5)
        concat5 = torch.cat((upsample5, pool4), dim=1)
        upsample4 = self._block4(concat5)
        concat4 = torch.cat((upsample4, pool3), dim=1)
        upsample3 = self._block5(concat4)
        concat3 = torch.cat((upsample3, pool2), dim=1)
        upsample2 = self._block5(concat3)
        concat2 = torch.cat((upsample2, pool1), dim=1)
        upsample1 = self._block5(concat2)
        concat1 = torch.cat((upsample1, x), dim=1)
        x = self._block6(concat1)

        if self.blindspot:
            # Apply shift
            shifted = self.shift(x)
            # Unstack, rotate and combine
            rotated_batch = torch.chunk(shifted, 4, dim=0)
            aligned = [
                ssdn.utils.rotate(rotated, rot)
                for rotated, rot in zip(rotated_batch, (0, 270, 180, 90))
            ]
            x = torch.cat(aligned, dim=1)

        x = self._block7(x)

        return x

    def input_wh_mul(self) -> int:
        """Multiple that both the width and height dimensions of an input must be to be
        processed by the network. This is devised from the number of pooling layers that
        reduce the input size.

        Returns:
            int: Dimension multiplier
        """
        max_pool_layers = 5
        return 2 ** max_pool_layers
