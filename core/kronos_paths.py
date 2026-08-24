"""
Kronos의 기본 predict()는 sample_count개 경로를 뽑은 뒤 평균만 돌려준다.
이 파이프라인은 경로별 분산(표준편차)이 필요하므로, 같은 내부 함수를
그대로 쓰되 평균을 취하기 전에 반환하도록 감싼 얇은 래퍼를 둔다.
모델 가중치·토크나이저·추론 로직은 100% 원본 shiyu-coder/Kronos 코드다.
"""
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import torch

# 기본값은 프로젝트 루트에 클론된 Kronos 레포. README 설치 절차가 여기에 두게 안내한다.
# KRONOS_REPO 환경변수로 다른 위치를 가리킬 수 있다.
_DEFAULT_REPO = Path(__file__).resolve().parent.parent / "Kronos"
_REPO = os.getenv("KRONOS_REPO") or str(_DEFAULT_REPO)
if not Path(_REPO).exists():
    raise ImportError(
        f"Kronos 레포를 찾을 수 없습니다: {_REPO}\n"
        "  git clone https://github.com/shiyu-coder/Kronos.git\n"
        "를 프로젝트 루트에서 실행하거나, KRONOS_REPO 환경변수로 경로를 지정하세요."
    )
sys.path.insert(0, _REPO)
from model.kronos import calc_time_stamps, sample_from_logits  # noqa: E402
from tqdm import trange


def _auto_regressive_inference_paths(tokenizer, model, x, x_stamp, y_stamp, max_context,
                                      pred_len, clip, T, top_k, top_p, sample_count, verbose):
    """model/kronos.py::auto_regressive_inference 과 동일하되, 마지막 np.mean(axis=1)을
    생략하고 (batch, sample_count, pred_len, feat) 전체 경로를 반환한다."""
    with torch.no_grad():
        x = torch.clip(x, -clip, clip)
        device = x.device
        x = x.unsqueeze(1).repeat(1, sample_count, 1, 1).reshape(-1, x.size(1), x.size(2)).to(device)
        x_stamp = x_stamp.unsqueeze(1).repeat(1, sample_count, 1, 1).reshape(-1, x_stamp.size(1), x_stamp.size(2)).to(device)
        y_stamp = y_stamp.unsqueeze(1).repeat(1, sample_count, 1, 1).reshape(-1, y_stamp.size(1), y_stamp.size(2)).to(device)

        x_token = tokenizer.encode(x, half=True)
        initial_seq_len = x.size(1)
        batch_size = x_token[0].size(0)
        total_seq_len = initial_seq_len + pred_len
        full_stamp = torch.cat([x_stamp, y_stamp], dim=1)

        generated_pre = x_token[0].new_empty(batch_size, pred_len)
        generated_post = x_token[1].new_empty(batch_size, pred_len)
        pre_buffer = x_token[0].new_zeros(batch_size, max_context)
        post_buffer = x_token[1].new_zeros(batch_size, max_context)
        buffer_len = min(initial_seq_len, max_context)
        if buffer_len > 0:
            start_idx = max(0, initial_seq_len - max_context)
            pre_buffer[:, :buffer_len] = x_token[0][:, start_idx:start_idx + buffer_len]
            post_buffer[:, :buffer_len] = x_token[1][:, start_idx:start_idx + buffer_len]

        ran = trange if verbose else range
        for i in ran(pred_len):
            current_seq_len = initial_seq_len + i
            window_len = min(current_seq_len, max_context)
            if current_seq_len <= max_context:
                input_tokens = [pre_buffer[:, :window_len], post_buffer[:, :window_len]]
            else:
                input_tokens = [pre_buffer, post_buffer]
            context_end = current_seq_len
            context_start = max(0, context_end - max_context)
            current_stamp = full_stamp[:, context_start:context_end, :].contiguous()

            s1_logits, context = model.decode_s1(input_tokens[0], input_tokens[1], current_stamp)
            s1_logits = s1_logits[:, -1, :]
            sample_pre = sample_from_logits(s1_logits, temperature=T, top_k=top_k, top_p=top_p, sample_logits=True)
            s2_logits = model.decode_s2(context, sample_pre)
            s2_logits = s2_logits[:, -1, :]
            sample_post = sample_from_logits(s2_logits, temperature=T, top_k=top_k, top_p=top_p, sample_logits=True)

            generated_pre[:, i] = sample_pre.squeeze(-1)
            generated_post[:, i] = sample_post.squeeze(-1)
            if current_seq_len < max_context:
                pre_buffer[:, current_seq_len] = sample_pre.squeeze(-1)
                post_buffer[:, current_seq_len] = sample_post.squeeze(-1)
            else:
                pre_buffer.copy_(torch.roll(pre_buffer, shifts=-1, dims=1))
                post_buffer.copy_(torch.roll(post_buffer, shifts=-1, dims=1))
                pre_buffer[:, -1] = sample_pre.squeeze(-1)
                post_buffer[:, -1] = sample_post.squeeze(-1)

        full_pre = torch.cat([x_token[0], generated_pre], dim=1)
        full_post = torch.cat([x_token[1], generated_post], dim=1)
        context_start = max(0, total_seq_len - max_context)
        input_tokens = [full_pre[:, context_start:total_seq_len].contiguous(),
                         full_post[:, context_start:total_seq_len].contiguous()]
        z = tokenizer.decode(input_tokens, half=True)
        z = z.reshape(-1, sample_count, z.size(1), z.size(2))
        preds = z.cpu().numpy()
        preds = preds[:, :, -pred_len:, :]
        return preds  # (batch=1, sample_count, pred_len, feat)


def predict_paths(predictor, df, x_timestamp, y_timestamp, pred_len, T=1.0, top_p=0.9,
                   sample_count=16, verbose=False):
    """KronosPredictor.predict()와 같은 전처리를 거치되, 평균이 아니라
    sample_count개의 경로를 각각 반환한다. 반환: (sample_count, pred_len, 4) 의
    [open, high, low, close] 배열."""
    price_cols = ["open", "high", "low", "close"]
    df = df.copy()
    if "volume" not in df.columns:
        df["volume"] = 0.0
        df["amount"] = 0.0
    if "amount" not in df.columns:
        df["amount"] = df["volume"] * df[price_cols].mean(axis=1)

    x_time_df = calc_time_stamps(x_timestamp)
    y_time_df = calc_time_stamps(y_timestamp)

    x = df[price_cols + ["volume", "amount"]].values.astype(np.float32)
    x_stamp = x_time_df.values.astype(np.float32)
    y_stamp = y_time_df.values.astype(np.float32)

    x_mean, x_std = np.mean(x, axis=0), np.std(x, axis=0)
    xn = (x - x_mean) / (x_std + 1e-5)
    xn = np.clip(xn, -predictor.clip, predictor.clip)
    xn = xn[np.newaxis, :]
    x_stamp = x_stamp[np.newaxis, :]
    y_stamp = y_stamp[np.newaxis, :]

    x_tensor = torch.from_numpy(np.array(xn).astype(np.float32)).to(predictor.device)
    x_stamp_tensor = torch.from_numpy(np.array(x_stamp).astype(np.float32)).to(predictor.device)
    y_stamp_tensor = torch.from_numpy(np.array(y_stamp).astype(np.float32)).to(predictor.device)

    preds = _auto_regressive_inference_paths(
        predictor.tokenizer, predictor.model, x_tensor, x_stamp_tensor, y_stamp_tensor,
        predictor.max_context, pred_len, predictor.clip, T, 0, top_p, sample_count, verbose,
    )
    preds = preds[0]  # (sample_count, pred_len, 6)
    preds = preds * (x_std + 1e-5) + x_mean
    return preds[:, :, :4]  # open/high/low/close only
