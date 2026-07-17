"""
pdf_epub_converter.py
----------------------
وحدة مستقلة لتحويل PDF <-> EPUB باستخدام أداة ebook-convert من Calibre.

هذا الملف مستقل تمامًا عن bot.py حاليًا، مصمم ليسهل اختباره وبرمجته
بمعزل، ثم ربطه لاحقًا داخل bot.py عبر استيراد الدالتين:
    from pdf_epub_converter import convert_pdf_to_epub, convert_epub_to_pdf

⚠️ متطلب أساسي: يجب تثبيت Calibre على النظام (توفر أمر `ebook-convert`).
لن يعمل هذا الملف بدون تثبيته أولًا.
"""

import asyncio
import shutil
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# اسم الأمر التنفيذي لـ Calibre، أو مسار كامل. يمكن تجاوزه عبر متغير بيئة
# CALIBRE_BIN إن كان مثبتًا في مكان غير اعتيادي.
_CANDIDATE_PATHS = [
    "ebook-convert",             # إن كان ضمن PATH
    "/opt/calibre/ebook-convert",
    "/usr/bin/ebook-convert",
    "/opt/bin/ebook-convert",
]


def _resolve_ebook_convert_bin() -> str:
    import os as _os
    env_override = _os.environ.get("CALIBRE_BIN")
    if env_override:
        return env_override
    for candidate in _CANDIDATE_PATHS:
        if shutil.which(candidate) or Path(candidate).is_file():
            return candidate
    return "ebook-convert"  # قيمة افتراضية، سيُكتشف عدم توفرها لاحقًا


EBOOK_CONVERT_BIN = _resolve_ebook_convert_bin()

# مهلة قصوى للتحويل (بالثواني) لتفادي تعليق العملية على ملفات كبيرة أو تالفة
CONVERSION_TIMEOUT = 300  # 5 دقائق


class EbookConversionError(Exception):
    """تُرفع عند فشل عملية التحويل."""


# ----------------------------------------------------------------------
# أدوات مساعدة
# ----------------------------------------------------------------------

def is_calibre_available() -> bool:
    """يتحقق مما إذا كان أمر ebook-convert متاحًا على النظام."""
    return shutil.which(EBOOK_CONVERT_BIN) is not None or Path(EBOOK_CONVERT_BIN).is_file()


async def _run_ebook_convert(input_path: Path, output_path: Path, *extra_args: str) -> None:
    """تشغيل ebook-convert بشكل غير متزامن مع مهلة زمنية قصوى."""
    if not is_calibre_available():
        raise EbookConversionError(
            "أداة ebook-convert غير مثبتة على هذا النظام. "
            "يجب تثبيت Calibre أولًا (راجع تعليمات الملف)."
        )

    cmd = [EBOOK_CONVERT_BIN, str(input_path), str(output_path), *extra_args]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=CONVERSION_TIMEOUT
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise EbookConversionError("انتهت المهلة الزمنية للتحويل (الملف كبير جدًا أو معقد).")

    if process.returncode != 0 or not output_path.exists():
        error_text = stderr.decode(errors="ignore")[-800:] or stdout.decode(errors="ignore")[-800:]
        raise EbookConversionError(f"فشل التحويل عبر Calibre: {error_text}")


# ----------------------------------------------------------------------
# الدوال العامة (يتم استيرادها داخل bot.py)
# ----------------------------------------------------------------------

async def convert_pdf_to_epub(input_path: Path, out_dir: Path) -> Path:
    """
    تحويل ملف PDF إلى EPUB.

    ملاحظة جودة: تحويل PDF -> EPUB هو الأصعب تقنيًا لأن PDF ذو تخطيط ثابت
    بينما EPUB يحتاج نصًا قابلًا لإعادة التدفق. النتيجة تكون جيدة مع الكتب
    النصية البسيطة، وقد تكون متوسطة الجودة مع الجداول أو الأعمدة المتعددة
    أو الخطوط المعقدة.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / (input_path.stem + ".epub")

    await _run_ebook_convert(
        input_path,
        output_path,
        # خيارات تحسّن جودة استخراج النص من PDF
        "--enable-heuristics",
    )
    return output_path


async def convert_epub_to_pdf(input_path: Path, out_dir: Path) -> Path:
    """
    تحويل ملف EPUB إلى PDF.
    هذا الاتجاه أسهل وأعلى جودة عادةً (من تنسيق مرن إلى تنسيق ثابت).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / (input_path.stem + ".pdf")

    await _run_ebook_convert(
        input_path,
        output_path,
        "--paper-size", "a4",
        "--pdf-page-margin-left", "36",
        "--pdf-page-margin-right", "36",
        "--pdf-page-margin-top", "36",
        "--pdf-page-margin-bottom", "36",
    )
    return output_path
