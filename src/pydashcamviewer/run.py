#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import bisect
import datetime
import struct
import time
import tkinter as tk
from tkinter import filedialog, messagebox

import cv2
from PIL import Image, ImageTk
from tkintermapview import TkinterMapView

from . import nvtk_mp42gpx


def read_mp4_creation_time(file_path: str, use_daylight_saving_time: bool = True) -> tuple[int, int, float, float]:
    with open(file_path, "rb") as f:
        data = f.read()

    mvhd_index = data.find(b"mvhd")
    if mvhd_index == -1:
        raise ValueError("No 'mvhd' atom found.")

    creation_time_offset = mvhd_index + 8
    timescale_offset = mvhd_index + 16
    duration_offset = mvhd_index + 20

    creation_time = struct.unpack(">I", data[creation_time_offset : creation_time_offset + 4])[0]
    epoch = datetime.datetime(1904, 1, 1)
    creation_datetime = epoch + datetime.timedelta(seconds=creation_time)
    epoch_time = int(creation_datetime.timestamp())

    is_dst = time.localtime(epoch_time).tm_isdst
    if use_daylight_saving_time and not is_dst:
        epoch_time -= 3600

    timescale = struct.unpack(">I", data[timescale_offset : timescale_offset + 4])[0]
    duration = struct.unpack(">I", data[duration_offset : duration_offset + 4])[0]
    duration_seconds = duration / timescale if timescale > 0 else 0.0

    stts_index = data.find(b"stts")
    if stts_index == -1:
        return epoch_time, is_dst, duration_seconds, 0.0

    entry_count_offset = stts_index + 8
    entry_count = struct.unpack(">I", data[entry_count_offset : entry_count_offset + 4])[0]

    total_samples = 0
    total_duration = 0
    for i in range(entry_count):
        sample_count_offset = entry_count_offset + 4 + (i * 8)
        sample_count = struct.unpack(">I", data[sample_count_offset : sample_count_offset + 4])[0]
        frame_duration = struct.unpack(">I", data[sample_count_offset + 4 : sample_count_offset + 8])[0]
        total_samples += sample_count
        total_duration += sample_count * frame_duration

    if total_duration <= 0 or timescale <= 0:
        return epoch_time, is_dst, duration_seconds, 0.0

    fps = total_samples / (total_duration / timescale)
    return epoch_time, is_dst, duration_seconds, fps


def extract_coordinates_from_mp4(
    file_path: str, use_daylight_saving_time: bool = True
) -> tuple[float, list[dict[str, float | str]]]:
    video_epoch_time, _, duration_seconds, _ = read_mp4_creation_time(
        file_path, use_daylight_saving_time=use_daylight_saving_time
    )
    video_start_epoch = video_epoch_time - duration_seconds
    positions = nvtk_mp42gpx.get_data_package(file_path)

    coordinates: list[dict[str, float | str]] = []
    for step in positions:
        coordinates.append(
            {
                "epoch": step["Epoch"],
                "lat": step["Loc"]["Lat"]["Float"],
                "lon": step["Loc"]["Lon"]["Float"],
                "speed": step["Loc"]["Speed"],
                "bear": step["Loc"]["Bearing"],
                "date": step["DT"]["DT"],
            }
        )

    return video_start_epoch, coordinates


def downsample_route(route: list[tuple[float, float]], max_points: int = 1200) -> list[tuple[float, float]]:
    if len(route) <= max_points:
        return route
    step = max(1, len(route) // max_points)
    reduced = route[::step]
    if reduced[-1] != route[-1]:
        reduced.append(route[-1])
    return reduced


class OpenCVVideoPlayer(tk.Frame):
    def __init__(
        self,
        master: tk.Misc,
        video_path: str,
        on_time_update,
        on_load_file,
        max_fps: float | None = None,
    ):
        super().__init__(master)
        self.video_path = video_path
        self.on_time_update = on_time_update
        self.on_load_file = on_load_file

        self.cap = cv2.VideoCapture(self.video_path)
        if not self.cap.isOpened():
            raise RuntimeError(f"Failed to open video file: {self.video_path}")

        source_fps = float(self.cap.get(cv2.CAP_PROP_FPS))
        if source_fps <= 0:
            source_fps = 30.0

        if max_fps and max_fps > 0:
            self.playback_fps = min(source_fps, max_fps)
        else:
            self.playback_fps = source_fps

        self.frame_interval_s = 1.0 / self.playback_fps
        self.frame_count = float(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.duration_ms = (self.frame_count / source_fps) * 1000.0 if self.frame_count > 0 else 0.0

        self.playing = False
        self._slider_internal_update = False
        self._next_frame_deadline = 0.0

        self.video_panel = tk.Label(self, bg="black")
        self.video_panel.grid(row=0, column=0, sticky="nsew")

        self.controls = tk.Frame(self)
        self.controls.grid(row=1, column=0, sticky="ew")
        self.controls.columnconfigure(2, weight=1)

        tk.Button(self.controls, text="Play", command=self.play).grid(row=0, column=0, padx=5, pady=5)
        tk.Button(self.controls, text="Pause", command=self.pause).grid(row=0, column=1, padx=5, pady=5)

        self.scale_var = tk.DoubleVar(value=0)
        self.slider = tk.Scale(
            self.controls,
            variable=self.scale_var,
            orient=tk.HORIZONTAL,
            from_=0,
            to=1000,
            length=300,
            command=self.on_slider,
        )
        self.slider.grid(row=0, column=2, padx=5, pady=5, sticky="ew")

        tk.Button(self.controls, text="Load File", command=self.on_load_file).grid(
            row=1, column=0, columnspan=2, padx=5, pady=5
        )

        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

    def play(self) -> None:
        if not self.playing:
            self.playing = True
            self._next_frame_deadline = time.perf_counter()
            self.update_frame()

    def pause(self) -> None:
        self.playing = False

    def close(self) -> None:
        self.playing = False
        if self.cap.isOpened():
            self.cap.release()

    def _render_frame(self, frame_bgr) -> None:
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)

        panel_width = self.video_panel.winfo_width()
        panel_height = self.video_panel.winfo_height()
        if panel_width > 1 and panel_height > 1:
            height, width = frame_rgb.shape[:2]
            scale = min(panel_width / width, panel_height / height)
            if scale > 0 and abs(scale - 1.0) > 0.01:
                frame_rgb = cv2.resize(
                    frame_rgb,
                    (max(1, int(width * scale)), max(1, int(height * scale))),
                    interpolation=cv2.INTER_LINEAR,
                )

        image = Image.fromarray(frame_rgb)
        imgtk = ImageTk.PhotoImage(image=image)
        self.video_panel.imgtk = imgtk
        self.video_panel.config(image=imgtk)

    def update_frame(self) -> None:
        if not self.playing:
            return

        now = time.perf_counter()
        if now < self._next_frame_deadline:
            wait_ms = max(1, int((self._next_frame_deadline - now) * 1000))
            self.after(wait_ms, self.update_frame)
            return

        ret, frame = self.cap.read()
        if not ret:
            self.playing = False
            return

        self._render_frame(frame)

        current_time_ms = self.cap.get(cv2.CAP_PROP_POS_MSEC)
        if self.duration_ms > 0:
            self._slider_internal_update = True
            self.scale_var.set((current_time_ms / self.duration_ms) * 1000)
            self._slider_internal_update = False

        self.on_time_update(current_time_ms / 1000.0)
        self._next_frame_deadline += self.frame_interval_s

        # Catch up if UI/rendering falls behind realtime.
        now = time.perf_counter()
        if now > (self._next_frame_deadline + self.frame_interval_s):
            lag_s = now - self._next_frame_deadline
            frames_to_drop = min(int(lag_s / self.frame_interval_s), int(self.playback_fps * 0.5))
            for _ in range(frames_to_drop):
                if not self.cap.grab():
                    break
            self._next_frame_deadline = now

        self.after(1, self.update_frame)

    def on_slider(self, value: str) -> None:
        if self._slider_internal_update:
            return

        if self.duration_ms <= 0:
            return

        try:
            val = float(value)
        except ValueError:
            val = 0.0

        new_time_ms = (val / 1000.0) * self.duration_ms
        self.cap.set(cv2.CAP_PROP_POS_MSEC, new_time_ms)
        self._next_frame_deadline = time.perf_counter()

        ret, frame = self.cap.read()
        if ret:
            self._render_frame(frame)
            current_time_ms = self.cap.get(cv2.CAP_PROP_POS_MSEC)
            self.on_time_update(current_time_ms / 1000.0)


class MapPanel(tk.Frame):
    def __init__(
        self,
        master: tk.Misc,
        coordinates: list[dict[str, float | str]],
        follow_map: bool,
        pan_interval_s: float,
    ):
        super().__init__(master)
        self.coordinates = coordinates
        self.follow_map = follow_map
        self.pan_interval_s = pan_interval_s

        self._last_pan_time = 0.0
        self._last_pan_position: tuple[float, float] | None = None

        route = [
            (float(step["lat"]), float(step["lon"]))
            for step in coordinates
            if float(step["lat"]) != 0.0 and float(step["lon"]) != 0.0
        ]
        route = downsample_route(route, max_points=1200)

        initial_lat = float(coordinates[0]["lat"])
        initial_lon = float(coordinates[0]["lon"])

        self.map_widget = TkinterMapView(self, corner_radius=0)
        self.map_widget.grid(row=0, column=0, sticky="nsew")
        self.map_widget.set_zoom(15)
        self.map_widget.set_position(initial_lat, initial_lon)

        if len(route) >= 2:
            self.map_widget.set_path(route)

        self.marker = self.map_widget.set_marker(initial_lat, initial_lon, text="Current position")
        self._last_pan_position = (initial_lat, initial_lon)

        info = tk.Frame(self)
        info.grid(row=1, column=0, sticky="ew")

        self.speed_kmh_var = tk.StringVar(value="0 km/h")
        self.speed_mps_var = tk.StringVar(value="0 m/s")
        self.lat_var = tk.StringVar(value=f"Lat.: {initial_lat}")
        self.lon_var = tk.StringVar(value=f"Lon.: {initial_lon}")
        self.time_var = tk.StringVar(value="-")

        speed_box = tk.LabelFrame(info, text="Speed")
        speed_box.grid(row=0, column=0, sticky="ew", padx=25, pady=5)
        tk.Label(speed_box, textvariable=self.speed_kmh_var).grid(row=0, column=0, sticky="w", padx=15, pady=2)
        tk.Label(speed_box, textvariable=self.speed_mps_var).grid(row=1, column=0, sticky="w", padx=15, pady=2)

        gps_box = tk.LabelFrame(info, text="GPS Position")
        gps_box.grid(row=0, column=1, sticky="ew", padx=25, pady=5)
        tk.Label(gps_box, textvariable=self.lat_var).grid(row=0, column=0, sticky="w", padx=15, pady=2)
        tk.Label(gps_box, textvariable=self.lon_var).grid(row=1, column=0, sticky="w", padx=15, pady=2)

        time_box = tk.LabelFrame(info, text="GPS Timestamp")
        time_box.grid(row=0, column=2, sticky="ew", padx=25, pady=5)
        tk.Label(time_box, textvariable=self.time_var).grid(row=0, column=0, sticky="w", padx=15, pady=2)

        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

    def update_location(self, coord: dict[str, float | str]) -> None:
        lat = float(coord["lat"])
        lon = float(coord["lon"])

        self.marker.set_position(lat, lon)

        if self.follow_map:
            now = time.perf_counter()
            if now - self._last_pan_time >= self.pan_interval_s:
                last_lat, last_lon = self._last_pan_position or (lat, lon)
                moved = abs(lat - last_lat) + abs(lon - last_lon)
                if moved >= 0.0002:
                    self.map_widget.set_position(lat, lon)
                    self._last_pan_position = (lat, lon)
                    self._last_pan_time = now

        speed_mps = float(coord["speed"])
        speed_kmh = speed_mps * 3.6

        self.speed_kmh_var.set(f"{speed_kmh:.2f} km/h")
        self.speed_mps_var.set(f"{speed_mps:.4f} m/s")
        self.lat_var.set(f"Lat.: {lat}")
        self.lon_var.set(f"Lon.: {lon}")
        self.time_var.set(str(coord["date"]))


class VideoMapApp(tk.Frame):
    def __init__(
        self,
        master: tk.Misc,
        video_path: str,
        video_start_epoch: float,
        coordinates: list[dict[str, float | str]],
        on_request_load_file,
        map_update_ms: int,
        follow_map: bool,
        pan_interval_s: float,
        max_fps: float | None,
    ):
        super().__init__(master)
        self.video_start_epoch = video_start_epoch
        self.coordinates = coordinates
        self.coordinate_epochs = [float(coord["epoch"]) for coord in coordinates]
        self.current_epoch = video_start_epoch
        self._last_map_idx: int | None = None
        self.map_update_ms = max(100, map_update_ms)

        self.video_player = OpenCVVideoPlayer(
            self,
            video_path,
            on_time_update=self._on_video_time_update,
            on_load_file=on_request_load_file,
            max_fps=max_fps,
        )
        self.video_player.grid(row=0, column=0, sticky="nsew")

        self.map_panel = MapPanel(self, coordinates, follow_map=follow_map, pan_interval_s=pan_interval_s)
        self.map_panel.grid(row=0, column=1, sticky="nsew")

        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)

        self.after(self.map_update_ms, self._update_map_marker)
        self.video_player.play()

    def _on_video_time_update(self, current_seconds: float) -> None:
        self.current_epoch = self.video_start_epoch + current_seconds

    def _nearest_coordinate(self) -> tuple[int, dict[str, float | str]] | None:
        if not self.coordinates:
            return None

        idx = bisect.bisect_left(self.coordinate_epochs, self.current_epoch)
        if idx <= 0:
            return 0, self.coordinates[0]

        if idx >= len(self.coordinate_epochs):
            last_idx = len(self.coordinate_epochs) - 1
            return last_idx, self.coordinates[last_idx]

        before_idx = idx - 1
        after_idx = idx
        before_diff = abs(self.current_epoch - self.coordinate_epochs[before_idx])
        after_diff = abs(self.coordinate_epochs[after_idx] - self.current_epoch)
        best_idx = before_idx if before_diff <= after_diff else after_idx
        return best_idx, self.coordinates[best_idx]

    def _update_map_marker(self) -> None:
        nearest = self._nearest_coordinate()
        if nearest is not None:
            idx, coord = nearest
            if idx != self._last_map_idx:
                self.map_panel.update_location(coord)
                self._last_map_idx = idx
        self.after(self.map_update_ms, self._update_map_marker)

    def close(self) -> None:
        self.video_player.close()


class DashcamViewer:
    def __init__(
        self,
        initial_video: str | None,
        use_daylight_saving_time: bool,
        map_update_ms: int,
        follow_map: bool,
        map_pan_interval_ms: int,
        max_fps: float | None,
    ):
        self.initial_video = initial_video
        self.use_daylight_saving_time = use_daylight_saving_time
        self.map_update_ms = map_update_ms
        self.follow_map = follow_map
        self.map_pan_interval_s = max(0.1, map_pan_interval_ms / 1000.0)
        self.max_fps = max_fps

        self.root = tk.Tk()
        self.root.title("Python Dashcam Player")
        self.root.geometry("1400x800")
        self.root.withdraw()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.app: VideoMapApp | None = None

    def _select_video(self) -> str:
        return filedialog.askopenfilename(
            title="Select an MP4 file",
            filetypes=[("MP4 files", "*.mp4")],
        )

    def _load_video(self, video_file: str) -> bool:
        try:
            video_start_epoch, coordinates = extract_coordinates_from_mp4(
                video_file,
                use_daylight_saving_time=self.use_daylight_saving_time,
            )
        except Exception as exc:
            messagebox.showerror("Failed to read video", str(exc))
            return False

        if not coordinates:
            messagebox.showerror("No GPS Data", "No GPS data found in the selected file.")
            return False

        if self.app is not None:
            self.app.close()
            self.app.destroy()

        self.app = VideoMapApp(
            self.root,
            video_file,
            video_start_epoch,
            coordinates,
            on_request_load_file=self.load_new_file,
            map_update_ms=self.map_update_ms,
            follow_map=self.follow_map,
            pan_interval_s=self.map_pan_interval_s,
            max_fps=self.max_fps,
        )
        self.app.grid(row=0, column=0, sticky="nsew")

        self.root.rowconfigure(0, weight=1)
        self.root.columnconfigure(0, weight=1)
        self.root.deiconify()
        return True

    def load_new_file(self) -> None:
        video_file = self._select_video()
        if not video_file:
            return
        self._load_video(video_file)

    def bootstrap(self) -> bool:
        video_file = self.initial_video or self._select_video()
        while video_file:
            if self._load_video(video_file):
                return True
            video_file = self._select_video()
        return False

    def run(self) -> None:
        self.root.mainloop()

    def _on_close(self) -> None:
        if self.app is not None:
            self.app.close()
        self.root.destroy()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pydashcamviewer",
        description="Play dashcam videos and visualize GPS position in real time.",
    )
    parser.add_argument(
        "video",
        nargs="?",
        help="optional path to an MP4 file; if omitted, a file chooser opens",
    )
    parser.add_argument(
        "--no-daylight-saving-time",
        action="store_false",
        dest="use_daylight_saving_time",
        help="disable the one-hour DST adjustment when parsing creation time",
    )
    parser.add_argument(
        "--map-update-ms",
        type=int,
        default=350,
        help="map marker refresh interval in milliseconds (default: 350)",
    )
    parser.add_argument(
        "--map-pan-interval-ms",
        type=int,
        default=1000,
        help="auto-center interval in milliseconds while following position (default: 1000)",
    )
    parser.add_argument(
        "--no-map-follow",
        action="store_false",
        dest="follow_map",
        help="do not auto-center the map while playback advances",
    )
    parser.add_argument(
        "--max-fps",
        type=float,
        default=0.0,
        help="cap playback/render FPS for smoother performance on slow machines (e.g. 30)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    cv2.setUseOptimized(True)

    args = parse_args(argv)
    max_fps = args.max_fps if args.max_fps > 0 else None

    viewer = DashcamViewer(
        initial_video=args.video,
        use_daylight_saving_time=args.use_daylight_saving_time,
        map_update_ms=args.map_update_ms,
        follow_map=args.follow_map,
        map_pan_interval_ms=args.map_pan_interval_ms,
        max_fps=max_fps,
    )
    if not viewer.bootstrap():
        return 1
    viewer.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
