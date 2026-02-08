import sys
import os
import json
import re
from pathlib import Path
from typing import Optional, List, Dict
from urllib.parse import urlparse, parse_qs
from dataclasses import dataclass

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QComboBox, QListWidget, QListWidgetItem,
    QFileDialog, QProgressBar, QMessageBox, QFrame, QScrollArea,
    QButtonGroup, QRadioButton, QCheckBox, QSizePolicy
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSize, QTimer, QUrl, QObject
from PyQt6.QtGui import QPixmap, QFont, QIcon, QPalette, QColor
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply

import yt_dlp


@dataclass
class VideoInfo:
    video_id: str
    title: str
    author: str
    duration: str
    thumbnail_url: str
    
    
class ThumbnailLoader(QThread):
    """Thread for loading thumbnails asynchronously"""
    thumbnail_loaded = pyqtSignal(str, QPixmap)
    
    def __init__(self, video_id: str, url: str):
        super().__init__()
        self.video_id = video_id
        self.url = url
        # Don't create QNetworkAccessManager here - it needs to be in the thread's run method
        
    def run(self):
        from PyQt6.QtCore import QEventLoop
        
        # Create the network manager in this thread's context
        manager = QNetworkAccessManager()
        loop = QEventLoop()
        
        request = QNetworkRequest(QUrl(self.url))
        reply = manager.get(request)
        
        def on_finished():
            if reply.error() == QNetworkReply.NetworkError.NoError:
                data = reply.readAll()
                pixmap = QPixmap()
                pixmap.loadFromData(data)
                if not pixmap.isNull():
                    scaled_pixmap = pixmap.scaled(160, 90, Qt.AspectRatioMode.KeepAspectRatio, 
                                                   Qt.TransformationMode.SmoothTransformation)
                    self.thumbnail_loaded.emit(self.video_id, scaled_pixmap)
            loop.quit()
            
        reply.finished.connect(on_finished)
        loop.exec()


class VideoInfoFetcher(QThread):
    """Thread for fetching video information"""
    info_fetched = pyqtSignal(object)
    error_occurred = pyqtSignal(str)
    playlist_fetched = pyqtSignal(list)
    
    def __init__(self, url: str):
        super().__init__()
        self.url = url
        
    def run(self):
        try:
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'extract_flat': 'in_playlist',
                'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
                'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            }
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(self.url, download=False)
                
                # Check if it's a playlist
                if 'entries' in info:
                    videos = []
                    for entry in info['entries']:
                        if entry:
                            video_id = entry.get('id', '')
                            video_info = VideoInfo(
                                video_id=video_id,
                                title=entry.get('title', 'Unknown Title'),
                                author=entry.get('uploader', 'Unknown'),
                                duration=self._format_duration(entry.get('duration', 0)),
                                thumbnail_url=f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
                            )
                            videos.append(video_info)
                    self.playlist_fetched.emit(videos)
                else:
                    # Single video
                    video_id = info.get('id', '')
                    video_info = VideoInfo(
                        video_id=video_id,
                        title=info.get('title', 'Unknown Title'),
                        author=info.get('uploader', 'Unknown'),
                        duration=self._format_duration(info.get('duration', 0)),
                        thumbnail_url=f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
                    )
                    self.info_fetched.emit(video_info)
                    
        except Exception as e:
            self.error_occurred.emit(str(e))
    
    def _format_duration(self, seconds):
        if not seconds:
            return "0:00"
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        if hours > 0:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes}:{secs:02d}"


class DownloadWorker(QThread):
    """Thread for downloading videos"""
    progress = pyqtSignal(str, float, str, str)  # video_id, percentage, speed, eta
    finished = pyqtSignal(str, bool, str)  # video_id, success, message
    
    def __init__(self, video_id: str, output_dir: str, format_type: str, quality: str, container_format: str):
        super().__init__()
        self.video_id = video_id
        self.output_dir = output_dir
        self.format_type = format_type
        self.quality = quality
        self.container_format = container_format
        self._is_running = True
        
    def run(self):
        try:
            url = f"https://www.youtube.com/watch?v={self.video_id}"
            
            # Build format options
            if self.format_type == "video":
                if self.quality == "highest":
                    format_string = "bestvideo+bestaudio/best"
                elif self.quality == "144p":
                    format_string = "worst"
                else:
                    # Extract height from quality string (e.g., "720p" -> 720)
                    height = self.quality.replace('p', '')
                    format_string = f"bestvideo[height<={height}]+bestaudio/best[height<={height}]"
            else:  # audio
                format_string = "bestaudio/best"
            
            ydl_opts = {
                'format': format_string,
                'outtmpl': os.path.join(self.output_dir, '%(title)s.%(ext)s'),
                'progress_hooks': [self._progress_hook],
                'quiet': True,
                'no_warnings': True,
                # Fix for 403 Forbidden errors
                'http_headers': {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Accept-Language': 'en-us,en;q=0.5',
                    'Sec-Fetch-Mode': 'navigate',
                },
                'extractor_args': {
                    'youtube': {
                        'player_client': ['android', 'web'],
                        'player_skip': ['webpage', 'configs']
                    }
                },
            }
            
            # Add postprocessing for format conversion
            if self.format_type == "audio":
                quality_map = {
                    "320 kbps": "320",
                    "256 kbps": "256",
                    "192 kbps": "192",
                    "128 kbps": "128",
                    "96 kbps": "96"
                }
                bitrate = quality_map.get(self.quality, "192")
                
                ydl_opts['postprocessors'] = [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': self.container_format,
                    'preferredquality': bitrate,
                }]
            else:
                # For video, merge and convert to desired format if needed
                ydl_opts['merge_output_format'] = self.container_format
                if self.container_format != 'mp4':
                    ydl_opts['postprocessors'] = [{
                        'key': 'FFmpegVideoConvertor',
                        'preferedformat': self.container_format,
                    }]
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                if self._is_running:
                    ydl.download([url])
                    self.finished.emit(self.video_id, True, "Download completed successfully!")
                    
        except Exception as e:
            self.finished.emit(self.video_id, False, f"Download failed: {str(e)}")
    
    def _progress_hook(self, d):
        if not self._is_running:
            raise Exception("Download cancelled")
            
        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
            downloaded = d.get('downloaded_bytes', 0)
            
            if total > 0:
                percentage = (downloaded / total) * 100
            else:
                percentage = 0
                
            speed = d.get('speed', 0)
            eta = d.get('eta', 0)
            
            speed_str = self._format_speed(speed)
            eta_str = self._format_time(eta)
            
            self.progress.emit(self.video_id, percentage, speed_str, eta_str)
    
    def _format_speed(self, speed):
        if not speed:
            return "0 B/s"
        if speed < 1024:
            return f"{speed:.0f} B/s"
        elif speed < 1024 * 1024:
            return f"{speed/1024:.1f} KB/s"
        else:
            return f"{speed/(1024*1024):.2f} MB/s"
    
    def _format_time(self, seconds):
        if not seconds or seconds < 0:
            return "Unknown"
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        if hours > 0:
            return f"{hours}h {minutes}m {secs}s"
        elif minutes > 0:
            return f"{minutes}m {secs}s"
        else:
            return f"{secs}s"
    
    def stop(self):
        self._is_running = False


class VideoQueueItem(QFrame):
    """Widget representing a video in the queue"""
    delete_clicked = pyqtSignal(str)
    selection_changed = pyqtSignal(str, bool)
    item_clicked = pyqtSignal(str)  # New signal for clicking to view details
    
    def __init__(self, video_info: VideoInfo):
        super().__init__()
        self.video_info = video_info
        self.is_selected = False
        self._setup_ui()
        
    def _setup_ui(self):
        self.setFrameStyle(QFrame.Shape.StyledPanel)
        self.setStyleSheet("""
            VideoQueueItem {
                background-color: #2b2b2b;
                border-radius: 8px;
                padding: 10px;
            }
            VideoQueueItem:hover {
                background-color: #353535;
            }
        """)
        # Remove cursor from stylesheet - let Qt handle it
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        
        # Checkbox for selection
        self.checkbox = QCheckBox()
        self.checkbox.setStyleSheet("QCheckBox::indicator { width: 20px; height: 20px; }")
        self.checkbox.stateChanged.connect(self._on_selection_changed)
        layout.addWidget(self.checkbox)
        
        # Thumbnail
        self.thumbnail_label = QLabel()
        self.thumbnail_label.setFixedSize(160, 90)
        self.thumbnail_label.setStyleSheet("background-color: #1a1a1a; border-radius: 4px;")
        self.thumbnail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumbnail_label.setText("Loading...")
        layout.addWidget(self.thumbnail_label)
        
        # Info layout
        info_layout = QVBoxLayout()
        
        # Title
        self.title_label = QLabel(self.video_info.title)
        self.title_label.setWordWrap(True)
        self.title_label.setStyleSheet("font-weight: bold; font-size: 14px; color: white;")
        info_layout.addWidget(self.title_label)
        
        # Author
        author_label = QLabel(f"by {self.video_info.author}")
        author_label.setStyleSheet("color: #aaaaaa; font-size: 12px;")
        info_layout.addWidget(author_label)
        
        # Duration
        duration_label = QLabel(f"Duration: {self.video_info.duration}")
        duration_label.setStyleSheet("color: #aaaaaa; font-size: 12px;")
        info_layout.addWidget(duration_label)
        
        # Progress bar (hidden initially)
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setMaximumHeight(20)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                border: 1px solid #404040;
                border-radius: 4px;
                background-color: #1a1a1a;
                text-align: center;
                color: white;
                font-size: 10px;
            }
            QProgressBar::chunk {
                background-color: #c41e3a;
                border-radius: 3px;
            }
        """)
        info_layout.addWidget(self.progress_bar)
        
        # Status label (hidden initially)
        self.status_label = QLabel("")
        self.status_label.setVisible(False)
        self.status_label.setStyleSheet("color: #aaaaaa; font-size: 10px;")
        info_layout.addWidget(self.status_label)
        
        info_layout.addStretch()
        layout.addLayout(info_layout, 1)
        
        # Delete button
        self.delete_btn = QPushButton("✕")
        self.delete_btn.setFixedSize(30, 30)
        self.delete_btn.setStyleSheet("""
            QPushButton {
                background-color: #c41e3a;
                color: white;
                border: none;
                border-radius: 15px;
                font-size: 16px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #e02041;
            }
        """)
        self.delete_btn.clicked.connect(lambda: self.delete_clicked.emit(self.video_info.video_id))
        layout.addWidget(self.delete_btn, alignment=Qt.AlignmentFlag.AlignTop)
        
        # Set cursor for hovering
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        
    def load_thumbnail(self, pixmap: QPixmap):
        self.thumbnail_label.setPixmap(pixmap)
        
    def set_selected(self, selected: bool):
        self.is_selected = selected
        self.checkbox.setChecked(selected)
        if selected:
            self.setStyleSheet("""
                VideoQueueItem {
                    background-color: #404040;
                    border: 2px solid #c41e3a;
                    border-radius: 8px;
                }
            """)
        else:
            self.setStyleSheet("""
                VideoQueueItem {
                    background-color: #2b2b2b;
                    border-radius: 8px;
                }
                VideoQueueItem:hover {
                    background-color: #353535;
                }
            """)
    
    def _on_selection_changed(self, state):
        is_checked = state == Qt.CheckState.Checked.value
        self.selection_changed.emit(self.video_info.video_id, is_checked)
    
    def mousePressEvent(self, event):
        # Only emit click if not clicking on checkbox or delete button
        if event.button() == Qt.MouseButton.LeftButton:
            # Check if click is on the main area (not checkbox or delete button)
            self.item_clicked.emit(self.video_info.video_id)
        super().mousePressEvent(event)
    
    def update_progress(self, percentage: float, speed: str, eta: str):
        """Update download progress for this item"""
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(int(percentage))
        self.status_label.setVisible(True)
        self.status_label.setText(f"Speed: {speed} | ETA: {eta}")
    
    def mark_completed(self):
        """Mark this download as completed"""
        self.progress_bar.setVisible(False)
        self.status_label.setVisible(True)
        self.status_label.setText("✓ Download completed")
        self.status_label.setStyleSheet("color: #4CAF50; font-size: 10px; font-weight: bold;")
    
    def mark_failed(self, error: str):
        """Mark this download as failed"""
        self.progress_bar.setVisible(False)
        self.status_label.setVisible(True)
        self.status_label.setText(f"✗ Failed: {error}")
        self.status_label.setStyleSheet("color: #f44336; font-size: 10px;")
    
    def reset_status(self):
        """Reset the status indicators"""
        self.progress_bar.setVisible(False)
        self.progress_bar.setValue(0)
        self.status_label.setVisible(False)
        self.status_label.setText("")


class YouTubeDownloader(QMainWindow):
    def __init__(self):
        super().__init__()
        self.video_queue: List[VideoInfo] = []
        self.queue_items: Dict[str, VideoQueueItem] = {}
        self.thumbnail_loaders: List[ThumbnailLoader] = []
        self.download_workers: List[DownloadWorker] = []  # Changed to list for multiple workers
        self.output_directory = str(Path.home() / "Downloads")
        self.is_queue_mode = False
        self.selected_videos: set = set()
        self.viewing_single_in_queue = False  # Track if viewing single video from queue
        self.active_downloads = 0
        self.total_downloads = 0
        self.download_progress: Dict[str, float] = {}  # Track individual progress
        
        self._setup_ui()
        self.setWindowTitle("YouTube Downloader")
        self.resize(1000, 700)
        
    def _setup_ui(self):
        # Apply dark theme
        self.setStyleSheet("""
            QMainWindow {
                background-color: #1a1a1a;
            }
            QLabel {
                color: white;
            }
            QLineEdit {
                background-color: #2b2b2b;
                color: white;
                border: 2px solid #404040;
                border-radius: 6px;
                padding: 8px;
                font-size: 14px;
            }
            QLineEdit:focus {
                border: 2px solid #c41e3a;
            }
            QPushButton {
                background-color: #c41e3a;
                color: white;
                border: none;
                border-radius: 6px;
                padding: 10px 20px;
                font-size: 14px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #e02041;
            }
            QPushButton:pressed {
                background-color: #a01830;
            }
            QPushButton:disabled {
                background-color: #404040;
                color: #888888;
            }
            QComboBox {
                background-color: #2b2b2b;
                color: white;
                border: 2px solid #404040;
                border-radius: 6px;
                padding: 8px;
                font-size: 14px;
            }
            QComboBox:hover {
                border: 2px solid #c41e3a;
            }
            QComboBox::drop-down {
                border: none;
            }
            QComboBox::down-arrow {
                image: none;
                border-left: 5px solid transparent;
                border-right: 5px solid transparent;
                border-top: 5px solid white;
                margin-right: 10px;
            }
            QComboBox QAbstractItemView {
                background-color: #2b2b2b;
                color: white;
                selection-background-color: #c41e3a;
                border: 2px solid #404040;
            }
            QRadioButton {
                color: white;
                font-size: 14px;
            }
            QRadioButton::indicator {
                width: 18px;
                height: 18px;
            }
            QRadioButton::indicator:unchecked {
                background-color: #2b2b2b;
                border: 2px solid #404040;
                border-radius: 9px;
            }
            QRadioButton::indicator:checked {
                background-color: #c41e3a;
                border: 2px solid #c41e3a;
                border-radius: 9px;
            }
            QProgressBar {
                border: 2px solid #404040;
                border-radius: 6px;
                background-color: #2b2b2b;
                text-align: center;
                color: white;
                font-weight: bold;
            }
            QProgressBar::chunk {
                background-color: #c41e3a;
                border-radius: 4px;
            }
            QScrollArea {
                border: none;
                background-color: #1a1a1a;
            }
        """)
        
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(15)
        
        # Title
        title = QLabel("YouTube Downloader")
        title.setStyleSheet("font-size: 28px; font-weight: bold; color: #c41e3a;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        main_layout.addWidget(title)
        
        # Format selection
        format_layout = QHBoxLayout()
        format_label = QLabel("Download Type:")
        format_label.setStyleSheet("font-size: 16px; font-weight: bold;")
        format_layout.addWidget(format_label)
        
        self.format_group = QButtonGroup()
        self.video_radio = QRadioButton("Video")
        self.audio_radio = QRadioButton("Audio")
        self.video_radio.setChecked(True)
        self.format_group.addButton(self.video_radio)
        self.format_group.addButton(self.audio_radio)
        
        self.video_radio.toggled.connect(self._on_format_changed)
        
        format_layout.addWidget(self.video_radio)
        format_layout.addWidget(self.audio_radio)
        format_layout.addStretch()
        main_layout.addLayout(format_layout)
        
        # Quality selection
        quality_layout = QHBoxLayout()
        quality_label = QLabel("Quality:")
        quality_label.setStyleSheet("font-size: 14px; font-weight: bold;")
        quality_layout.addWidget(quality_label)
        
        self.quality_combo = QComboBox()
        self.quality_combo.setMinimumWidth(150)
        self._update_quality_options()
        quality_layout.addWidget(self.quality_combo)
        
        # Format/Container selection
        format_label = QLabel("Format:")
        format_label.setStyleSheet("font-size: 14px; font-weight: bold;")
        quality_layout.addWidget(format_label)
        
        self.format_combo = QComboBox()
        self.format_combo.setMinimumWidth(100)
        self._update_format_options()
        quality_layout.addWidget(self.format_combo)
        
        quality_layout.addStretch()
        main_layout.addLayout(quality_layout)
        
        # URL input
        url_layout = QHBoxLayout()
        url_label = QLabel("Video/Playlist URL:")
        url_label.setStyleSheet("font-size: 14px; font-weight: bold;")
        url_layout.addWidget(url_label)
        
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Paste YouTube URL here...")
        url_layout.addWidget(self.url_input)
        
        self.fetch_btn = QPushButton("Fetch")
        self.fetch_btn.clicked.connect(self._fetch_video_info)
        url_layout.addWidget(self.fetch_btn)
        
        main_layout.addLayout(url_layout)
        
        # Mode toggle (Single/Queue)
        mode_layout = QHBoxLayout()
        self.mode_label = QLabel("Mode: Single Video")
        self.mode_label.setStyleSheet("font-size: 14px; font-weight: bold;")
        mode_layout.addWidget(self.mode_label)
        
        self.toggle_mode_btn = QPushButton("Switch to Queue Mode")
        self.toggle_mode_btn.clicked.connect(self._toggle_mode)
        mode_layout.addWidget(self.toggle_mode_btn)
        
        # Back button (hidden initially)
        self.back_btn = QPushButton("← Back to Queue")
        self.back_btn.clicked.connect(self._back_to_queue)
        self.back_btn.setVisible(False)
        mode_layout.addWidget(self.back_btn)
        
        mode_layout.addStretch()
        main_layout.addLayout(mode_layout)
        
        # Queue actions (hidden initially)
        self.queue_actions_widget = QWidget()
        queue_actions_layout = QHBoxLayout(self.queue_actions_widget)
        queue_actions_layout.setContentsMargins(0, 0, 0, 0)
        
        self.delete_selected_btn = QPushButton("Delete Selected")
        self.delete_selected_btn.clicked.connect(self._delete_selected)
        self.delete_selected_btn.setVisible(False)
        queue_actions_layout.addWidget(self.delete_selected_btn)
        
        self.cancel_selection_btn = QPushButton("Cancel Selection")
        self.cancel_selection_btn.clicked.connect(self._cancel_selection)
        self.cancel_selection_btn.setVisible(False)
        queue_actions_layout.addWidget(self.cancel_selection_btn)
        
        queue_actions_layout.addStretch()
        self.queue_actions_widget.setVisible(False)
        main_layout.addWidget(self.queue_actions_widget)
        
        # Video display area
        self.video_display = QScrollArea()
        self.video_display.setWidgetResizable(True)
        self.video_display.setMinimumHeight(300)
        
        self.video_container = QWidget()
        self.video_layout = QVBoxLayout(self.video_container)
        self.video_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.video_display.setWidget(self.video_container)
        
        main_layout.addWidget(self.video_display, 1)
        
        # Output directory
        output_layout = QHBoxLayout()
        output_label = QLabel("Output Directory:")
        output_label.setStyleSheet("font-size: 14px; font-weight: bold;")
        output_layout.addWidget(output_label)
        
        self.output_path_label = QLabel(self.output_directory)
        self.output_path_label.setStyleSheet("color: #aaaaaa;")
        output_layout.addWidget(self.output_path_label, 1)
        
        browse_btn = QPushButton("Browse")
        browse_btn.clicked.connect(self._browse_output)
        output_layout.addWidget(browse_btn)
        
        main_layout.addLayout(output_layout)
        
        # Download button and progress
        self.download_btn = QPushButton("Download")
        self.download_btn.setEnabled(False)
        self.download_btn.clicked.connect(self._start_download)
        self.download_btn.setMinimumHeight(50)
        self.download_btn.setStyleSheet("""
            QPushButton {
                background-color: #c41e3a;
                font-size: 16px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #e02041;
            }
            QPushButton:disabled {
                background-color: #404040;
            }
        """)
        main_layout.addWidget(self.download_btn)
        
        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setMinimumHeight(30)
        main_layout.addWidget(self.progress_bar)
        
        # Status label
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #aaaaaa; font-size: 12px;")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        main_layout.addWidget(self.status_label)
        
    def _on_format_changed(self):
        self._update_quality_options()
        self._update_format_options()
        
    def _update_format_options(self):
        """Update the format/container options based on audio/video selection"""
        self.format_combo.clear()
        if self.video_radio.isChecked():
            self.format_combo.addItems([
                "mp4",
                "mkv",
                "webm",
                "avi",
                "mov"
            ])
        else:
            self.format_combo.addItems([
                "mp3",
                "m4a",
                "opus",
                "wav",
                "flac"
            ])
        
    def _update_quality_options(self):
        self.quality_combo.clear()
        if self.video_radio.isChecked():
            self.quality_combo.addItems([
                "highest",
                "1080p",
                "720p",
                "480p",
                "360p",
                "240p",
                "144p"
            ])
        else:
            self.quality_combo.addItems([
                "320 kbps",
                "256 kbps",
                "192 kbps",
                "128 kbps",
                "96 kbps"
            ])
    
    def _fetch_video_info(self):
        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "Error", "Please enter a YouTube URL")
            return
        
        self.fetch_btn.setEnabled(False)
        self.fetch_btn.setText("Fetching...")
        
        self.fetcher = VideoInfoFetcher(url)
        self.fetcher.info_fetched.connect(self._on_video_info_fetched)
        self.fetcher.playlist_fetched.connect(self._on_playlist_fetched)
        self.fetcher.error_occurred.connect(self._on_fetch_error)
        self.fetcher.finished.connect(lambda: self.fetch_btn.setEnabled(True))
        self.fetcher.finished.connect(lambda: self.fetch_btn.setText("Fetch"))
        self.fetcher.start()
    
    def _on_video_info_fetched(self, video_info: VideoInfo):
        if self.is_queue_mode:
            self._add_to_queue(video_info)
        else:
            # Clear and show single video
            self._clear_video_display()
            self.video_queue = [video_info]
            self._display_single_video(video_info)
            self.download_btn.setEnabled(True)
    
    def _on_playlist_fetched(self, videos: List[VideoInfo]):
        self._clear_video_display()
        self.video_queue = videos
        self.is_queue_mode = True
        self.viewing_single_in_queue = False
        self.mode_label.setText(f"Mode: Queue ({len(videos)} videos)")
        self.toggle_mode_btn.setText("Switch to Single Mode")
        self.back_btn.setVisible(False)
        
        for video in videos:
            self._add_queue_item(video)
        
        self.download_btn.setEnabled(len(videos) > 0)
        self.queue_actions_widget.setVisible(True)
    
    def _on_fetch_error(self, error_msg: str):
        QMessageBox.critical(self, "Error", f"Failed to fetch video info:\n{error_msg}")
    
    def _display_single_video(self, video_info: VideoInfo):
        # Create a nice single video display
        container = QFrame()
        container.setStyleSheet("""
            QFrame {
                background-color: #2b2b2b;
                border-radius: 12px;
                padding: 20px;
            }
        """)
        
        layout = QVBoxLayout(container)
        
        # Thumbnail
        thumbnail_label = QLabel()
        thumbnail_label.setFixedSize(480, 270)
        thumbnail_label.setStyleSheet("background-color: #1a1a1a; border-radius: 8px;")
        thumbnail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        thumbnail_label.setText("Loading thumbnail...")
        layout.addWidget(thumbnail_label, alignment=Qt.AlignmentFlag.AlignCenter)
        
        # Load thumbnail
        loader = ThumbnailLoader(video_info.video_id, video_info.thumbnail_url)
        loader.thumbnail_loaded.connect(lambda vid, pix: thumbnail_label.setPixmap(
            pix.scaled(480, 270, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        ))
        loader.start()
        self.thumbnail_loaders.append(loader)
        
        # Title
        title_label = QLabel(video_info.title)
        title_label.setWordWrap(True)
        title_label.setStyleSheet("font-size: 20px; font-weight: bold; color: white;")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title_label)
        
        # Author and duration
        info_label = QLabel(f"by {video_info.author} • {video_info.duration}")
        info_label.setStyleSheet("font-size: 14px; color: #aaaaaa;")
        info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(info_label)
        
        self.video_layout.addWidget(container)
    
    def _add_to_queue(self, video_info: VideoInfo):
        if video_info.video_id not in [v.video_id for v in self.video_queue]:
            self.video_queue.append(video_info)
            self._add_queue_item(video_info)
            self.mode_label.setText(f"Mode: Queue ({len(self.video_queue)} videos)")
            self.download_btn.setEnabled(True)
    
    def _add_queue_item(self, video_info: VideoInfo):
        item = VideoQueueItem(video_info)
        item.delete_clicked.connect(self._remove_from_queue)
        item.selection_changed.connect(self._on_item_selection_changed)
        item.item_clicked.connect(self._on_queue_item_clicked)  # Connect click signal
        self.video_layout.addWidget(item)
        self.queue_items[video_info.video_id] = item
        
        # Load thumbnail
        loader = ThumbnailLoader(video_info.video_id, video_info.thumbnail_url)
        loader.thumbnail_loaded.connect(lambda vid, pix: self._on_thumbnail_loaded(vid, pix))
        loader.start()
        self.thumbnail_loaders.append(loader)
    
    def _on_thumbnail_loaded(self, video_id: str, pixmap: QPixmap):
        if video_id in self.queue_items:
            self.queue_items[video_id].load_thumbnail(pixmap)
    
    def _remove_from_queue(self, video_id: str):
        # Remove from queue
        self.video_queue = [v for v in self.video_queue if v.video_id != video_id]
        
        # Remove widget
        if video_id in self.queue_items:
            item = self.queue_items[video_id]
            self.video_layout.removeWidget(item)
            item.deleteLater()
            del self.queue_items[video_id]
        
        # Remove from selection
        self.selected_videos.discard(video_id)
        
        # Update UI
        if len(self.video_queue) == 0:
            self.download_btn.setEnabled(False)
            self.queue_actions_widget.setVisible(False)
            self.is_queue_mode = False
            self.viewing_single_in_queue = False
            self.mode_label.setText("Mode: Single Video")
            self.toggle_mode_btn.setText("Switch to Queue Mode")
            self.back_btn.setVisible(False)
        else:
            count_text = "video" if len(self.video_queue) == 1 else "videos"
            self.mode_label.setText(f"Mode: Queue ({len(self.video_queue)} {count_text})")
        
        self._update_selection_buttons()
    
    def _on_item_selection_changed(self, video_id: str, selected: bool):
        if selected:
            self.selected_videos.add(video_id)
        else:
            self.selected_videos.discard(video_id)
        
        self._update_selection_buttons()
    
    def _update_selection_buttons(self):
        has_selection = len(self.selected_videos) > 0
        self.delete_selected_btn.setVisible(has_selection)
        self.cancel_selection_btn.setVisible(has_selection)
    
    def _delete_selected(self):
        for video_id in list(self.selected_videos):
            self._remove_from_queue(video_id)
        self.selected_videos.clear()
        self._update_selection_buttons()
    
    def _cancel_selection(self):
        for video_id in list(self.selected_videos):
            if video_id in self.queue_items:
                self.queue_items[video_id].set_selected(False)
        self.selected_videos.clear()
        self._update_selection_buttons()
    
    def _on_queue_item_clicked(self, video_id: str):
        """Handle clicking on a queue item to view its details"""
        if not self.is_queue_mode:
            return
        
        # Find the video info
        video_info = None
        for video in self.video_queue:
            if video.video_id == video_id:
                video_info = video
                break
        
        if not video_info:
            return
        
        # Switch to single video view
        self.viewing_single_in_queue = True
        self._clear_video_display()
        self._display_single_video(video_info)
        
        # Update UI
        self.back_btn.setVisible(True)
        self.toggle_mode_btn.setVisible(False)
        self.queue_actions_widget.setVisible(False)
    
    def _back_to_queue(self):
        """Return to queue view from single video view"""
        if not self.viewing_single_in_queue or not self.is_queue_mode:
            return
        
        self.viewing_single_in_queue = False
        self._clear_video_display()
        
        # Restore queue view
        for video in self.video_queue:
            self._add_queue_item(video)
        
        # Update UI
        self.back_btn.setVisible(False)
        self.toggle_mode_btn.setVisible(True)
        self.queue_actions_widget.setVisible(True)
    
    def _clear_video_display(self):
        while self.video_layout.count():
            child = self.video_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        self.queue_items.clear()
        self.selected_videos.clear()
    
    def _toggle_mode(self):
        if not self.video_queue:
            return
        
        self.is_queue_mode = not self.is_queue_mode
        self.viewing_single_in_queue = False
        
        if self.is_queue_mode:
            # Switch to queue mode
            count_text = "video" if len(self.video_queue) == 1 else "videos"
            self.mode_label.setText(f"Mode: Queue ({len(self.video_queue)} {count_text})")
            self.toggle_mode_btn.setText("Switch to Single Mode")
            self.queue_actions_widget.setVisible(True)
            self.back_btn.setVisible(False)
            
            # Show queue view
            self._clear_video_display()
            for video in self.video_queue:
                self._add_queue_item(video)
        else:
            # Switch to single mode
            self.mode_label.setText("Mode: Single Video")
            self.toggle_mode_btn.setText("Switch to Queue Mode")
            self.queue_actions_widget.setVisible(False)
            self.back_btn.setVisible(False)
            
            # Show only first video in single mode
            self._clear_video_display()
            if self.video_queue:
                self._display_single_video(self.video_queue[0])
    
    def _browse_output(self):
        directory = QFileDialog.getExistingDirectory(self, "Select Output Directory", self.output_directory)
        if directory:
            self.output_directory = directory
            self.output_path_label.setText(directory)
    
    def _start_download(self):
        if not self.video_queue:
            return
        
        format_type = "video" if self.video_radio.isChecked() else "audio"
        quality = self.quality_combo.currentText()
        container_format = self.format_combo.currentText()
        
        if self.is_queue_mode and not self.viewing_single_in_queue:
            # Download all videos in queue simultaneously
            self.download_btn.setEnabled(False)
            self.progress_bar.setVisible(True)
            self.progress_bar.setValue(0)
            
            self.active_downloads = len(self.video_queue)
            self.total_downloads = len(self.video_queue)
            self.download_progress.clear()
            self.download_workers.clear()
            
            self.status_label.setText(f"Starting {self.total_downloads} downloads...")
            
            # Start a worker for each video
            for video in self.video_queue:
                worker = DownloadWorker(
                    video.video_id,
                    self.output_directory,
                    format_type,
                    quality,
                    container_format
                )
                worker.progress.connect(self._on_download_progress)
                worker.finished.connect(self._on_download_finished)
                self.download_workers.append(worker)
                worker.start()
                
                # Initialize progress tracking
                self.download_progress[video.video_id] = 0.0
        else:
            # Single video download
            video = self.video_queue[0]
            
            self.download_btn.setEnabled(False)
            self.progress_bar.setVisible(True)
            self.progress_bar.setValue(0)
            self.status_label.setText("Starting download...")
            
            self.active_downloads = 1
            self.total_downloads = 1
            self.download_progress.clear()
            self.download_workers.clear()
            
            worker = DownloadWorker(
                video.video_id,
                self.output_directory,
                format_type,
                quality,
                container_format
            )
            worker.progress.connect(self._on_download_progress)
            worker.finished.connect(self._on_download_finished)
            self.download_workers.append(worker)
            worker.start()
            
            self.download_progress[video.video_id] = 0.0
    
    def _on_download_progress(self, video_id: str, percentage: float, speed: str, eta: str):
        # Update individual video progress
        self.download_progress[video_id] = percentage
        
        # Update queue item if it exists
        if video_id in self.queue_items:
            self.queue_items[video_id].update_progress(percentage, speed, eta)
        
        # Update main progress bar with average progress
        if self.download_progress:
            avg_progress = sum(self.download_progress.values()) / len(self.download_progress)
            self.progress_bar.setValue(int(avg_progress))
            
            # Update status with active downloads count
            completed = self.total_downloads - self.active_downloads
            if self.is_queue_mode and not self.viewing_single_in_queue:
                self.status_label.setText(f"Downloading {self.active_downloads} files... ({completed}/{self.total_downloads} completed)")
            else:
                self.status_label.setText(f"Speed: {speed} | ETA: {eta}")
    
    def _on_download_finished(self, video_id: str, success: bool, message: str):
        self.active_downloads -= 1
        
        # Update queue item status
        if video_id in self.queue_items:
            if success:
                self.queue_items[video_id].mark_completed()
            else:
                self.queue_items[video_id].mark_failed(message.replace("Download failed: ", ""))
        
        # Check if all downloads are complete
        if self.active_downloads <= 0:
            self.download_btn.setEnabled(True)
            self.progress_bar.setVisible(False)
            
            # Count successes and failures
            all_success = all(
                self.queue_items[v.video_id].status_label.text().startswith("✓")
                for v in self.video_queue
                if v.video_id in self.queue_items
            )
            
            if all_success:
                self.status_label.setText(f"All {self.total_downloads} downloads completed successfully!")
                QMessageBox.information(self, "Success", f"All {self.total_downloads} downloads completed successfully!")
                
                # Remove completed videos from queue if in queue mode
                if self.is_queue_mode and not self.viewing_single_in_queue:
                    for video_id in list(self.queue_items.keys()):
                        self._remove_from_queue(video_id)
            else:
                self.status_label.setText(f"Downloads completed with some errors. Check individual statuses.")
                QMessageBox.warning(self, "Completed with Errors", 
                                   f"{self.total_downloads - self.active_downloads} downloads completed, but some had errors. Check the queue for details.")
        else:
            # Update status with remaining downloads
            completed = self.total_downloads - self.active_downloads
            self.status_label.setText(f"Downloading {self.active_downloads} files... ({completed}/{self.total_downloads} completed)")


def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    
    # Set dark palette
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(26, 26, 26))
    palette.setColor(QPalette.ColorRole.WindowText, Qt.GlobalColor.white)
    palette.setColor(QPalette.ColorRole.Base, QColor(43, 43, 43))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(53, 53, 53))
    palette.setColor(QPalette.ColorRole.ToolTipBase, Qt.GlobalColor.white)
    palette.setColor(QPalette.ColorRole.ToolTipText, Qt.GlobalColor.white)
    palette.setColor(QPalette.ColorRole.Text, Qt.GlobalColor.white)
    palette.setColor(QPalette.ColorRole.Button, QColor(43, 43, 43))
    palette.setColor(QPalette.ColorRole.ButtonText, Qt.GlobalColor.white)
    palette.setColor(QPalette.ColorRole.Link, QColor(196, 30, 58))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(196, 30, 58))
    palette.setColor(QPalette.ColorRole.HighlightedText, Qt.GlobalColor.white)
    app.setPalette(palette)
    
    window = YouTubeDownloader()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
