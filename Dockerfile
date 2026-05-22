# Dockerfile để build APK mà không phải cài Java/Android SDK trên máy
# Dùng:
#   docker build -t ll47-builder .
#   docker run --rm -v "$(pwd)/build:/app/build" ll47-builder
# Output APK sẽ ở thư mục build/apk/app-release.apk trên máy thật

FROM cirrusci/flutter:stable

# Cài Python + Java (image này đã có Flutter + Android SDK sẵn)
RUN apt-get update && apt-get install -y \
    python3 python3-pip python3-venv \
    openjdk-17-jdk \
    && rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
ENV PATH=$JAVA_HOME/bin:$PATH

WORKDIR /app
COPY requirements.txt ./
RUN pip3 install -r requirements.txt --break-system-packages

COPY . .

# Build APK khi run container
CMD ["flet", "build", "apk", "--project", "ll47_e141", "--org", "vn.mil.e141"]
