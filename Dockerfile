FROM python:3.11-slim

# تثبيت الاعتماديات الأساسية:
# - libreoffice: لتحويل Word<->PDF
# - ffmpeg: لتحويل الصوت
# - المكتبات المطلوبة لتشغيل Calibre في وضع headless (بدون واجهة رسومية)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libreoffice \
        ffmpeg \
        fonts-dejavu \
        fonts-liberation \
        wget \
        ca-certificates \
        xz-utils \
        libgl1 \
        libxkbcommon0 \
        libegl1 \
        libopengl0 \
        libxcb-cursor0 \
        libxcb-icccm4 \
        libxcb-image0 \
        libxcb-keysyms1 \
        libxcb-randr0 \
        libxcb-render-util0 \
        libxcb-shape0 \
        libxcb-xinerama0 \
        libnss3 \
        libfontconfig1 \
        libdbus-1-3 \
    && rm -rf /var/lib/apt/lists/*

# تثبيت Calibre في مسار ثابت ومعروف (/opt/calibre) عبر سكربت التثبيت الرسمي.
# نستخدم install_dir صراحة بدل الاعتماد على تكامل سطح المكتب الذي قد يفشل
# بصمت داخل حاويات Docker الأساسية.
RUN wget -nv -O /tmp/calibre-installer.sh https://download.calibre-ebook.com/linux-installer.sh && \
    sh /tmp/calibre-installer.sh install_dir=/opt && \
    rm -f /tmp/calibre-installer.sh

# نضيف مسار Calibre مباشرة إلى PATH بدل الاعتماد فقط على الروابط الرمزية،
# لضمان أن أمر ebook-convert يعمل دائمًا بغض النظر عن سلوك سكربت التثبيت.
ENV PATH="/opt/calibre:${PATH}"

# فحص إلزامي أثناء البناء: إن فشل تثبيت Calibre سيفشل البناء هنا مباشرة
# مع رسالة خطأ واضحة في سجل Railway، بدل ظهور المشكلة لاحقًا وقت التشغيل.
RUN ebook-convert --version

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p downloads converted

CMD ["python", "bot.py"]
