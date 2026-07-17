"""
pdf_epub_converter.py
----------------------
وحدة مستقلة لتحويل PDF <-> EPUB باستخدام أداة ebook-convert من Calibre.

هذا الملف مستقل تمامًا عن bot.py حاليًا، مصمم ليسهل اختباره وبرمجته
بمعزل، ثم ربطه لاحقًا داخل bot.py عبر استيراد الدالتين:
    from pdf_epub_converter import convert_pdf_to_epub, convert_epub_to_pdf

⚠️ متطلب أساسي: يجب تثبيت Calibre على النظام (توفر أمر `ebook-convert`).
لن يعمل هذا الملف بدون تثبيته أولًا.

طريقة تثبيت Calibre على Docker (Debian/Ubuntu) - الطريقة الموصى بها للأنظمة
الخادمية بدون واجهة رسومية (headless)، لأن نسخة apt غالبًا قديمة أو ناقصة:

    RUN apt-get update && apt-get install -y --no-install-recommends \
            libgl1 libxkbcommon0 libegl1 wget xz-utils \
        && rm -rf /var/lib/apt/lists/* \
        && wget -nv -O- https://download.calibre-ebook.com/linux-installer.sh | sh /dev/stdin

بعد التثبيت سيتوفر الأمر `ebook-convert` على المسار /opt/calibre/ebook-convert
(السكربت أعلاه يضيفه تلقائيًا إلى /usr/bin).
"""

import asyncio
import shutil
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# اسم الأمر التنفيذي لـ Calibre. عدّله هنا إذا كان مثبتًا بمسار مختلف
# (مثلاً "/opt/calibre/ebook-convert").
EBOOK_CONVERT_BIN = "ebook-convert"

# مهلة قصوى للتحويل (بالثواني) لتفادي تعليق العملية على ملفات كبيرة أو تالفة
CONVERSION_TIMEOUT = 300  # 5 دقائق


class EbookConversionError(Exception):
    """تُرفع عند فشل عملية التحويل."""


# ----------------------------------------------------------------------
# أدوات مساعدة
# ----------------------------------------------------------------------

def is_calibre_available() -> bool:
    """يتحقق مما إذا كان أمر ebook-convert متاحًا على النظام."""
    return shutil.which(EBOOK_CONVERT_BIN) is not None


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
# الدوال العامة (هذه ما سيتم استيراده داخل bot.py لاحقًا)
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


# ----------------------------------------------------------------------
# اختبار سريع من سطر الأوامر (بمعزل عن البوت)
# الاستخدام:
#   python pdf_epub_converter.py to_epub input.pdf ./output
#   python pdf_epub_converter.py to_pdf input.epub ./output
# ----------------------------------------------------------------------

async def _cli_main():
    import sys

    if len(sys.argv) != 4:
        print("الاستخدام:")
        print("  python pdf_epub_converter.py to_epub input.pdf output_dir")
        print("  python pdf_epub_converter.py to_pdf input.epub output_dir")
        sys.exit(1)

    mode, input_file, output_dir = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])

    if not input_file.exists():
        print(f"❌ الملف غير موجود: {input_file}")
        sys.exit(1)

    if not is_calibre_available():
        print("❌ Calibre (ebook-convert) غير مثبت على هذا النظام.")
        sys.exit(1)

    try:
        if mode == "to_epub":
            result = await convert_pdf_to_epub(input_file, output_dir)
        elif mode == "to_pdf":
            result = await convert_epub_to_pdf(input_file, output_dir)
        else:
            print("❌ الوضع غير معروف. استخدم to_epub أو to_pdf.")
            sys.exit(1)

        print(f"✅ تم التحويل بنجاح: {result}")
    except EbookConversionError as e:
        print(f"❌ فشل التحويل: {e}")
        sys.exit(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_cli_main())
