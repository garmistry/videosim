#!/usr/bin/env bash
set -euo pipefail

as_root() {
  if [[ "${EUID}" -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

install_linux() {
  if command -v apt-get >/dev/null 2>&1; then
    install_apt
  elif command -v dnf >/dev/null 2>&1; then
    install_dnf || {
      echo "dnf install failed. Fedora may need RPM Fusion enabled for ffmpeg/GStreamer extras."
      exit 1
    }
  elif command -v pacman >/dev/null 2>&1; then
    install_pacman
  else
    echo "Unsupported Linux distro: install python, tkinter, ffmpeg, and GStreamer."
    exit 1
  fi
}

install_macos() {
  if ! command -v brew >/dev/null 2>&1; then
    echo "Homebrew is required on macOS: https://brew.sh/"
    exit 1
  fi

  brew install python-tk@3.14 ffmpeg gstreamer
}

install_apt() {
  as_root apt-get update
  as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3 \
    python3-tk \
    python3-venv \
    ffmpeg \
    gstreamer1.0-tools \
    gstreamer1.0-x \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav
}

install_dnf() {
  as_root dnf install -y \
    python3 \
    python3-tkinter \
    ffmpeg \
    gstreamer1-tools \
    gstreamer1-plugins-base \
    gstreamer1-plugins-good \
    gstreamer1-plugins-bad-free \
    gstreamer1-plugins-bad-freeworld \
    gstreamer1-plugins-ugly \
    gstreamer1-libav
}

install_pacman() {
  as_root pacman -Sy --needed --noconfirm \
    python \
    tk \
    ffmpeg \
    gstreamer \
    gst-plugins-base \
    gst-plugins-good \
    gst-plugins-bad \
    gst-plugins-ugly \
    gst-libav
}

case "$(uname -s)" in
  Linux) install_linux ;;
  Darwin) install_macos ;;
  *)
    echo "Unsupported OS: install python, tkinter, ffmpeg, and GStreamer."
    exit 1
    ;;
esac

missing=0

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing command: $1"
    missing=1
  fi
}

require_gst_element() {
  if ! gst-inspect-1.0 "$1" >/dev/null 2>&1; then
    echo "Missing GStreamer element: $1"
    missing=1
  fi
}

require_python_tk() {
  local py
  for py in python3 python3.14 python3.13 python; do
    if command -v "${py}" >/dev/null 2>&1 && "${py}" -c "import tkinter" >/dev/null 2>&1; then
      echo "Python/Tk OK: ${py}"
      return
    fi
  done

  echo "Missing Python with tkinter"
  missing=1
}

require_python_tk
require_cmd ffmpeg
require_cmd ffprobe
require_cmd gst-launch-1.0
require_cmd gst-inspect-1.0

if command -v gst-inspect-1.0 >/dev/null 2>&1; then
  require_gst_element srtsink
  require_gst_element srtsrc
  require_gst_element mpegtsmux
  require_gst_element clockoverlay
  require_gst_element x264enc
  require_gst_element ccconverter
  require_gst_element h264ccinserter
fi

if [[ "${missing}" -ne 0 ]]; then
  echo "Dependency install finished, but required media pieces are still missing."
  exit 1
fi

echo "Video Feed Simulator dependencies installed."
