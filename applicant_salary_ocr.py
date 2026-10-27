"""Offline rendered-text evidence only. OCR never changes confirmed job conditions."""
import hashlib
import importlib.util
import math
from io import BytesIO
from pathlib import Path

import applicant_jobs
from sandbox import KernelError, current_execution


def recognize(png):
    current_execution('runtime.applicant.salary.read')
    if not isinstance(png, bytes) or not png.startswith(b'\x89PNG\r\n\x1a\n') or len(png) > 512 * 1024:
        raise KernelError('INVALID_SALARY_IMAGE', '薪资截图格式或容量无法核对。')
    try:
        from PIL import Image
        with Image.open(BytesIO(png)) as image:
            if image.format != 'PNG' or not (1 <= image.width <= 1200 and 1 <= image.height <= 240):
                raise ValueError('invalid salary image dimensions')
            image.verify()
    except (OSError, ValueError):
        raise KernelError('INVALID_SALARY_IMAGE', '薪资截图内容或尺寸无法核对。') from None
    # The installed package ships its models. Missing models are an explicit error;
    # no model download, external OCR service or alternate backend is attempted.
    spec = importlib.util.find_spec('rapidocr_onnxruntime')
    if spec is None:
        raise KernelError('OCR_UNAVAILABLE', '本机离线文字识别依赖尚未就绪。')
    root = Path(spec.origin).parent
    required = ('ch_PP-OCRv4_det_infer.onnx', 'ch_PP-OCRv4_rec_infer.onnx', 'ch_ppocr_mobile_v2.0_cls_infer.onnx')
    if any(not (root / 'models' / name).is_file() for name in required):
        raise KernelError('OCR_UNAVAILABLE', '本机离线文字识别模型不完整。')
    try:
        from rapidocr_onnxruntime import RapidOCR
        engine = RapidOCR(intra_op_num_threads=1, inter_op_num_threads=1)
        output, _ = engine(png, use_det=False, use_cls=False)
    except (ImportError, OSError):
        raise KernelError('OCR_UNAVAILABLE', '本机离线文字识别依赖无法加载。') from None
    if (not isinstance(output, list) or len(output) != 1 or not isinstance(output[0], (list, tuple))
            or len(output[0]) != 2 or not isinstance(output[0][0], str)
            or len(output[0][0]) > 80 or type(output[0][1]) not in (int, float)
            or not math.isfinite(output[0][1]) or not 0 <= output[0][1] <= 1):
        return {'status': 'unreadable', 'recognized_text': '', 'confidence': None, 'monthly_salary': None,
                'confirmed': False, 'image_sha256': hashlib.sha256(png).hexdigest()}
    text, confidence = output[0]
    monthly = applicant_jobs.monthly_salary(text)
    return {'status': 'needs_confirmation' if monthly and confidence >= 0.98 else 'needs_review',
            'recognized_text': text, 'confidence': float(confidence), 'monthly_salary': list(monthly) if monthly else None,
            'confirmed': False, 'image_sha256': hashlib.sha256(png).hexdigest()}
