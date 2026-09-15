"""Deterministic Docker stand-in so unit tests obtain real VerifiedImage receipts."""

import asyncio
import json

from recollect.selfmod.deployment import BundleImages, bundle_tar

BASE = "sha256:" + "a" * 64


class FakeImages:
    def __init__(self):
        self.images = {BASE: {"labels": {},
                              "layers": ["sha256:base-1", "sha256:base-2"],
                              "tar": b""}}

    def add(self, bundle, image_id, *, labels=None, layers=None, tar=None):
        base = self.images[bundle.base_image_id]["layers"]
        self.images[image_id] = {
            "labels": labels if labels is not None else {
                "recollect.bundle": bundle.digest,
                "recollect.candidate": bundle.candidate.sha256},
            "layers": (layers if layers is not None
                       else [*base, "sha256:" + image_id[7:15]]),
            "tar": tar if tar is not None else bundle_tar(bundle),
        }

    async def run(self, *args, data=None):
        if args[:2] == ("image", "inspect"):
            item = self.images.get(args[2])
            if item is None:
                return 1, b"", b"missing"
            return 0, json.dumps([{"Id": args[2], "Config": {"Labels": item["labels"]},
                                   "RootFS": {"Layers": item["layers"]}}]).encode(), b""
        if args[0] == "create":
            self.created = args[-1]
            return 0, b"f" * 64 + b"\n", b""
        if args[0] == "cp":
            return 0, self.images[self.created]["tar"], b""
        if args[0] == "rm":
            return 0, b"", b""
        raise AssertionError(args)


def verified(bundle, image_id, fake=None):
    fake = fake or FakeImages()
    if image_id not in fake.images:
        fake.add(bundle, image_id)
    return asyncio.run(BundleImages(fake).verify(bundle, image_id))
