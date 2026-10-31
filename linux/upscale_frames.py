"""Real-ESRGAN (RRDBNet x4) frame upscaler running on the ComfyUI venv's torch.

  python upscale_frames.py --in frames_in --out frames_out --scale 4 --batch 4
  python upscale_frames.py --single in.png --out out.png
"""
import argparse
import os
import sys

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from PIL import Image


class ResidualDenseBlock(nn.Module):
    def __init__(self, nf=64, gc=32):
        super().__init__()
        self.conv1 = nn.Conv2d(nf, gc, 3, 1, 1)
        self.conv2 = nn.Conv2d(nf + gc, gc, 3, 1, 1)
        self.conv3 = nn.Conv2d(nf + 2 * gc, gc, 3, 1, 1)
        self.conv4 = nn.Conv2d(nf + 3 * gc, gc, 3, 1, 1)
        self.conv5 = nn.Conv2d(nf + 4 * gc, nf, 3, 1, 1)
        self.lrelu = nn.LeakyReLU(0.2, inplace=True)

    def forward(self, x):
        x1 = self.lrelu(self.conv1(x))
        x2 = self.lrelu(self.conv2(torch.cat((x, x1), 1)))
        x3 = self.lrelu(self.conv3(torch.cat((x, x1, x2), 1)))
        x4 = self.lrelu(self.conv4(torch.cat((x, x1, x2, x3), 1)))
        x5 = self.conv5(torch.cat((x, x1, x2, x3, x4), 1))
        return x5 * 0.2 + x


class RRDB(nn.Module):
    def __init__(self, nf=64, gc=32):
        super().__init__()
        self.rdb1 = ResidualDenseBlock(nf, gc)
        self.rdb2 = ResidualDenseBlock(nf, gc)
        self.rdb3 = ResidualDenseBlock(nf, gc)

    def forward(self, x):
        return self.rdb3(self.rdb2(self.rdb1(x))) * 0.2 + x


class RRDBNet(nn.Module):
    def __init__(self, nf=64, nb=23, gc=32, scale=4, num_in_ch=3, up_stages=2):
        super().__init__()
        self.unshuffle = num_in_ch != 3
        self.conv_first = nn.Conv2d(num_in_ch, nf, 3, 1, 1)
        self.up_stages = up_stages
        self.body = nn.Sequential(*[RRDB(nf, gc) for _ in range(nb)])
        self.conv_body = nn.Conv2d(nf, nf, 3, 1, 1)
        self.conv_up1 = nn.Conv2d(nf, nf, 3, 1, 1)
        self.conv_up2 = nn.Conv2d(nf, nf, 3, 1, 1) if up_stages >= 2 else None
        self.conv_hr = nn.Conv2d(nf, nf, 3, 1, 1)
        self.conv_last = nn.Conv2d(nf, 3, 3, 1, 1)
        self.lrelu = nn.LeakyReLU(0.2, inplace=True)
        self.scale = scale

    def forward(self, x):
        if self.unshuffle:
            x = F.pixel_unshuffle(x, 2)
        feat = self.conv_first(x)
        feat = feat + self.conv_body(self.body(feat))
        feat = self.lrelu(self.conv_up1(F.interpolate(feat, scale_factor=2, mode="nearest")))
        if self.conv_up2 is not None:
            feat = self.lrelu(self.conv_up2(F.interpolate(feat, scale_factor=2, mode="nearest")))
        return self.conv_last(self.lrelu(self.conv_hr(feat)))


torch.backends.cudnn.benchmark = True


def load_model(path, device, scale=4):
    sd = torch.load(path, map_location="cpu", weights_only=True)
    if "params_ema" in sd:
        sd = sd["params_ema"]
    elif "params" in sd:
        sd = sd["params"]
    num_in_ch = int(sd["conv_first.weight"].shape[1])
    up_stages = 2 if "conv_up2.weight" in sd else 1
    net = RRDBNet(scale=scale, num_in_ch=num_in_ch, up_stages=up_stages)
    net.load_state_dict(sd, strict=True)
    net = net.eval().to(device).to(memory_format=torch.channels_last)


def to_tensor(img):
    a = np.asarray(img.convert("RGB"), dtype=np.float32) / 255.0
    return torch.from_numpy(a).permute(2, 0, 1).unsqueeze(0)


def to_image(t):
    a = t.squeeze(0).permute(1, 2, 0).clamp(0, 1).mul(255).round().byte().cpu().numpy()
    return Image.fromarray(a, "RGB")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--weights", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                     "tools", "RealESRGAN_x4plus.pth"))
    p.add_argument("--single")
    p.add_argument("--in", dest="indir")
    p.add_argument("--out", dest="out", required=True)
    p.add_argument("--batch", type=int, default=1)
    p.add_argument("--scale", type=int, default=4)
    p.add_argument("--half", action="store_true", default=True)
    a = p.parse_args()

    device = "cuda" if torch.cuda.is_available() else "cpu"
    net = load_model(a.weights, device, a.scale)
    if a.half and device == "cuda":
        net = net.half()

    if a.single:
        img = Image.open(a.single)
        t = to_tensor(img).to(device).contiguous(memory_format=torch.channels_last)
        t = t.half() if (a.half and device == "cuda") else t
        with torch.inference_mode():
            out = net(t)
        to_image(out.float()).save(a.out)
        print(f"{a.single} -> {a.out} {Image.open(a.out).size}")
        return

    os.makedirs(a.out, exist_ok=True)
    files = sorted(f for f in os.listdir(a.indir) if f.lower().endswith((".png", ".jpg", ".jpeg")))
    print(f"{len(files)} frames, device={device}, batch={a.batch}")
    for i in range(0, len(files), a.batch):
        chunk = files[i:i + a.batch]
        imgs = [Image.open(os.path.join(a.indir, f)) for f in chunk]
        ts = torch.cat([to_tensor(im) for im in imgs], 0).to(device).contiguous(memory_format=torch.channels_last)
        ts = ts.half() if (a.half and device == "cuda") else ts
        with torch.inference_mode():
            outs = net(ts)
        for f, o in zip(chunk, outs):
            to_image(o.float()).save(os.path.join(a.out, f.replace(".jpg", ".png")))
        done = min(i + a.batch, len(files))
        if done % 10 == 0 or done == len(files):
            print(f"  {done}/{len(files)}", flush=True)


if __name__ == "__main__":
    main()