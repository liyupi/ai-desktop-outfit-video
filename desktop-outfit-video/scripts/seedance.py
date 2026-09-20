#!/usr/bin/env python3
"""调用火山方舟 Seedance 2.5 生成桌面换装视频。

准备：
    pip install arkruntime requests
    export ARK_API_KEY=你的key

用法：
    python seedance.py --prompt-file prompt.txt
    python seedance.py --prompt-file prompt.txt --image /path/to/ref.jpg
    python seedance.py --model doubao-seedance-2-5-260628 --prompt-file prompt.txt

分辨率：
    文生视频默认 1080p（官方已上线）。
    带参考图时默认 720p——官方参数表写明参考图场景不支持 1080p。
    可用 --resolution 强制覆盖。1080p 若 InvalidParameter / InternalServiceError，自动降 720p 再试一次。

不要传 camera_fixed / optimize_prompt / watermark：Seedance 2.5 会 400 或内部错误。
"""

from __future__ import annotations

import argparse
import base64
import mimetypes
import os
import sys
import time
from pathlib import Path

import requests
from arkruntime import Ark
from arkruntime._exceptions import ArkAPIError, ArkNotFoundError

POLL_INTERVAL = 10
POLL_TIMEOUT = 900
DEFAULT_MODEL = "doubao-seedance-2-5-260628"
FALLBACK_MODELS = ("doubao-seedance-2-5-260628", "doubao-seedance-2-5")


def to_image_url(src: str) -> str:
    path = Path(src)
    if path.is_file():
        mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{data}"
    return src


def build_content(prompt: str, images: list[str]) -> list[dict]:
    content: list[dict] = [{"type": "text", "text": prompt}]
    for src in images:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": to_image_url(src)},
                "role": "reference_image",
            }
        )
    return content


def wait_for_task(client: Ark, task_id: str) -> object:
    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        task = client.content_generation.tasks.get(task_id)
        if task.status in ("succeeded", "failed", "cancelled"):
            return task
        print(f"  {task.status} ... {int(time.time() - deadline + POLL_TIMEOUT)}s", flush=True)
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"任务 {task_id} 超过 {POLL_TIMEOUT}s 仍未完成")


def error_text(exc: BaseException) -> str:
    return str(exc)


def is_model_missing(exc: BaseException) -> bool:
    text = error_text(exc)
    return isinstance(exc, ArkNotFoundError) or "InvalidEndpointOrModel.NotFound" in text


def is_privacy_image(exc: BaseException) -> bool:
    text = error_text(exc)
    return "InputImageSensitiveContentDetected.PrivacyInformation" in text or "may contain real person" in text


def should_fallback_720p(exc: BaseException | None, task_error: object | None = None) -> bool:
    chunks = [error_text(exc)] if exc else []
    if task_error is not None:
        chunks.append(str(task_error))
    text = " ".join(chunks)
    return (
        "InternalServiceError" in text
        or "InvalidParameter" in text
        or "resolution" in text.lower()
    )


def create_task(client: Ark, model: str, content: list[dict], args: argparse.Namespace, resolution: str):
    return client.content_generation.tasks.create(
        model=model,
        content=content,
        duration=args.duration,
        ratio=args.ratio,
        resolution=resolution,
        seed=args.seed,
        generate_audio=True,
    )


def submit_with_fallbacks(
    client: Ark,
    models: list[str],
    content: list[dict],
    args: argparse.Namespace,
    resolution: str,
):
    last_error: BaseException | None = None
    for model in models:
        try:
            created = create_task(client, model, content, args, resolution)
            print(f"任务已提交：{created.id}  model={model}  resolution={resolution}", flush=True)
            return created, model, resolution
        except ArkAPIError as exc:
            last_error = exc
            if is_privacy_image(exc):
                print(
                    "参考图被判定为真人肖像，接口拒绝直接上传。"
                    "不要原图重试。改用文字锁脸，或走方舟 LAS 素材库 asset://。",
                    file=sys.stderr,
                )
                raise
            if is_model_missing(exc):
                print(f"接入点不可用：{model}，换下一个", flush=True)
                continue
            if resolution == "1080p" and should_fallback_720p(exc):
                print(f"1080p 提交失败（{exc}），降到 720p 重试一次", flush=True)
                created = create_task(client, model, content, args, "720p")
                print(f"任务已提交：{created.id}  model={model}  resolution=720p", flush=True)
                return created, model, "720p"
            raise
    assert last_error is not None
    raise last_error


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=DEFAULT_MODEL, help="方舟接入点 ID，默认预置 doubao-seedance-2-5-260628")
    parser.add_argument("--prompt-file", required=True, type=Path)
    parser.add_argument("--image", action="append", default=[], help="参考图 URL 或本地路径，可重复")
    parser.add_argument("--duration", type=int, default=30)
    parser.add_argument("--ratio", default="16:9")
    parser.add_argument(
        "--resolution",
        default=None,
        help="默认：文生视频 1080p，带参考图 720p（参考图场景官方不支持 1080p）",
    )
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--out", type=Path, default=Path("output.mp4"))
    args = parser.parse_args()

    if not os.environ.get("ARK_API_KEY"):
        print("缺少 ARK_API_KEY 环境变量", file=sys.stderr)
        return 1

    prompt = args.prompt_file.read_text(encoding="utf-8").strip()
    client = Ark.volc()
    content = build_content(prompt, args.image)

    if args.resolution:
        resolution = args.resolution
    else:
        resolution = "720p" if args.image else "1080p"

    models: list[str] = []
    for candidate in (args.model, *FALLBACK_MODELS):
        if candidate not in models:
            models.append(candidate)

    try:
        created, model, resolution = submit_with_fallbacks(client, models, content, args, resolution)
    except ArkAPIError:
        return 1

    task = wait_for_task(client, created.id)
    if task.status != "succeeded" and resolution == "1080p" and should_fallback_720p(None, task.error):
        print(f"1080p 失败（{task.error}），降到 720p 重试一次", flush=True)
        try:
            created, model, resolution = submit_with_fallbacks(client, [model], content, args, "720p")
        except ArkAPIError:
            return 1
        task = wait_for_task(client, created.id)

    if task.status != "succeeded":
        print(f"生成失败：{task.status} {task.error}", file=sys.stderr)
        return 1

    url = task.content.video_url
    print(f"生成成功，下载中：{url}", flush=True)
    resp = requests.get(url, timeout=300)
    resp.raise_for_status()
    args.out.write_bytes(resp.content)
    print(f"已保存到 {args.out}（{len(resp.content) / 1e6:.1f} MB）  {resolution}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
