"""VintageCam Digitizer — analog video capture and calibration instruments.

If you're new to the code, read the modules in this order:

    video_format.py   what a 480i NTSC frame *is* (sizes, frame rates, pixel shape)
    color.py          how the bytes in a frame map to brightness and colour
    frames.py         how frames are handed between threads without lag
    capture.py        the one thread that owns the capture device
    recorder.py       the thread that writes lossless files
    dshow.py          talking directly to the card's driver (proc amp, TV standard)
    ui/               everything you can see and click
"""

__version__ = "0.1.0"
APP_NAME = "VintageCam Digitizer"
