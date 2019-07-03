# Copyright 2018 The Lucid Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""Method for saving arbitrary data to arbitrary destinations.

This module takes an object and URL, infers how to serialize and how to write
it out to the destination. The intention is to preserve your work under most
circumstances, so sometimes this will convert values by default and warn rather than
error out immediately. This sometimes means less predictable behavior.

If an object could have multiple serializations, this tries to infer the
intended serializations from the URL's file extension.

Possible extension: if not given a URL this could create one and return it?
"""

from __future__ import absolute_import, division, print_function

import sys
import logging
import subprocess
import warnings
from copy import copy

# from concurrent.futures import ThreadPoolExecutor
import os.path
import json
import numpy as np
import PIL.Image

from lucid.misc.io.writing import write_handle
from lucid.misc.io.serialize_array import _normalize_array


# create logger with module name, e.g. lucid.misc.io.saving
log = logging.getLogger(__name__)

this = sys.modules[__name__]
this.save_contexts = []


class CaptureSaveContext:
    """Keeps captured save results.
    Usage:
    save_context = CaptureSaveContext()
    with save_context:
        ...
    captured_results = save_context.captured_saves
    """

    def __init__(self):
        self.captured_saves = []

    def __enter__(self):
        self.previous_save_contexts = copy(this.save_contexts)
        this.save_contexts.append(self)

    def __exit__(self, exc_type, exc_value, traceback):
        # assert self in this.save_contexts and this.save_contexts[-1] == self
        # this.save_contexts.pop()
        this.save_contexts = self.previous_save_contexts

    def capture(self, save_result):
        if save_result is not None:
            self.captured_saves.append(save_result)


class ClarityJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (tuple, set)):
            return list(obj)
        elif isinstance(obj, np.integer):
            return int(obj)
        elif isinstance(obj, np.floating):
            return float(obj)
        elif isinstance(obj, np.ndarray):
            return obj.tolist()
        elif hasattr(obj, "to_json"):
            return obj.to_json()
        else:
            return super(ClarityJSONEncoder, self).default(obj)


# this.threadpool = None


# def _get_threadpool():
#     if this.threadpool is None:
#         this.threadpool = ThreadPoolExecutor(max_workers=8)
#     return this.threadpool


def save_json(object, handle, indent=2):
    """Save object as json on CNS."""
    obj_json = json.dumps(object, indent=indent, cls=ClarityJSONEncoder)
    handle.write(obj_json)

    return {"type": "json", "url": handle.name}


def save_npy(object, handle):
    """Save numpy array as npy file."""
    np.save(handle, object)

    return {"type": "npy", "shape": object.shape, "url": handle.name}


def save_npz(object, handle):
    """Save dict of numpy array as npz file."""
    # there is a bug where savez doesn't actually accept a file handle.
    log.warning("Saving npz files currently only works locally. :/")
    path = handle.name
    handle.close()
    if type(object) is dict:
        np.savez(path, **object)
    elif type(object) is list:
        np.savez(path, *object)
    else:
        log.warning("Saving non dict or list as npz file, did you maybe want npy?")
        np.savez(path, object)


def save_img(object, handle, domain=None, **kwargs):
    """Save numpy array as image file on CNS."""

    if isinstance(object, np.ndarray):
        normalized = _normalize_array(object, domain=domain)
        object = PIL.Image.fromarray(normalized)

    if isinstance(object, PIL.Image.Image):
        object.save(handle, **kwargs)  # will infer format from handle's url ext.
    else:
        raise ValueError("Can only save_img for numpy arrays or PIL.Images!")

    return {
        "type": "image",
        "shape": object.size + (len(object.getbands()),),
        "url": handle.name,
    }


def save_txt(object, handle, **kwargs):
    if isinstance(object, str):
        handle.write(object)
    elif isinstance(object, list):
        for line in object:
            if isinstance(line, str):
                line = line.encode()
            if not isinstance(line, bytes):
                line_type = type(line)
                line = repr(line).encode()
                warnings.warn(
                    "`save_txt` found an object of type {}; using `repr` to convert it to string.".format(
                        line_type
                    )
                )
            if not line.endswith(b"\n"):
                line += b"\n"
            handle.write(line)

    return {"type": "txt", "url": handle.name}


def save_str(object, handle, **kwargs):
    assert isinstance(object, str)
    handle.write(object)


def save_pb(object, handle, **kwargs):
    try:
        handle.write(object.SerializeToString())
    except AttributeError:
        warnings.warn(
            "`save_protobuf` failed for object {}. Re-raising original exception.".format(
                object
            )
        )
        raise


savers = {
    ".png": save_img,
    ".jpg": save_img,
    ".jpeg": save_img,
    ".webp": save_img,
    ".npy": save_npy,
    ".npz": save_npz,
    ".json": save_json,
    ".txt": save_txt,
    ".pb": save_pb,
}


# def _set_contexts(f, *args, context=None, **kwargs):
#     assert context
#     this.save_contexts = context
#     f(*args, **kwargs)


def save(thing, url_or_handle, **kwargs):
    """Save object to file on CNS.

    File format is inferred from path. Use save_img(), save_npy(), or save_json()
    if you need to force a particular format.

    Args:
      obj: object to save.
      path: CNS path.

    Raises:
      RuntimeError: If file extension not supported.
    """
    # send to background thread pool if requested and return promise
    # if asynchronous:
    #     return _get_threadpool().submit(
    #         _set_contexts,
    #         save,
    #         thing,
    #         url_or_handle,
    #         asynchronous=False,
    #         **kwargs,
    #         context=this.save_contexts,
    #     )

    # Determine context
    # Is this a handle? What is the extension? Are we saving to GCS?
    is_handle = hasattr(url_or_handle, "write") and hasattr(url_or_handle, "name")
    if is_handle:
        path = url_or_handle.name
    else:
        path = url_or_handle

    _, ext = os.path.splitext(path)
    is_gcs = path.startswith("gs://")

    if not ext:
        raise RuntimeError("No extension in URL: " + path)

    # Determine which saver should be used
    if ext in savers:
        saver = savers[ext]
    elif isinstance(thing, str):
        saver = save_str
    else:
        message = "Unknown extension '{}'. As a result, only strings can be saved, not {}. Supported extensions: {}"
        raise ValueError(message.format(ext, type(thing).__name__, list(savers.keys())))

    # Actually save
    if is_handle:
        result = saver(thing, url_or_handle, **kwargs)
    else:
        with write_handle(url_or_handle) as handle:
            result = saver(thing, handle, **kwargs)

    # Set mime type on gcs if html -- usually, when one saves an html to GCS,
    # they want it to be viewsable as a website.
    if is_gcs and ext == ".html":
        subprocess.run(
            ["gsutil", "setmeta", "-h", "Content-Type: text/html; charset=utf-8", path]
        )
    if is_gcs and ext == ".json":
        subprocess.run(
            ["gsutil", "setmeta", "-h", "Content-Type: application/json", path]
        )

    # capture save if a save context is available
    if this.save_contexts:
        log.debug(
            f"capturing save: resulted in {result} -> {path} in save_context {this.save_contexts[-1]}"
        )
        this.save_contexts[-1].capture(result)
    # else:
    #     log.debug(
    #         f"NOT capturing save: resulted in {result} -> {path} (save_contexts: {this.save_contexts})"
    #     )

    return result
