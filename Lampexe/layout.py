import tkinter as tk

TWIPS_PER_PIXEL = 15


def tw(twips: int) -> int:
    return round(twips / TWIPS_PER_PIXEL)


VB_FORM_BG = "SystemButtonFace"
VB_FRAME_BG = "SystemButtonFace"
VB_FIELD_BG = "SystemWindow"

VB_FONT = ("MS Sans Serif", 8)
VB_FONT_BOLD = ("MS Sans Serif", 8, "bold")


class ModeAwareMixin:

    def __init__(self):
        self._tagged_widgets = []

    def register_view_tag(self, widget, mode_tag, left, top, width=None, height=None):
        place_kwargs = place(widget, left, top, width, height)
        self._tagged_widgets.append((widget, mode_tag, place_kwargs))

    def apply_mode(self, mode: str):
        for widget, mode_tag, place_kwargs in self._tagged_widgets:
            if mode_tag is None or mode_tag == mode:
                widget.place(**place_kwargs)
            else:
                widget.place_forget()


def place(widget, left, top, width=None, height=None):
    kwargs = {"x": tw(left), "y": tw(top)}
    if width is not None:
        kwargs["width"] = tw(width)
    if height is not None:
        kwargs["height"] = tw(height)
    widget.place(**kwargs)
    return kwargs


def client_size(width_twips, height_twips):
    return tw(width_twips), tw(height_twips)


def make_frame(parent, caption, left, top, width, height, font=VB_FONT_BOLD):
    frame = tk.LabelFrame(parent, text=caption, bg=VB_FRAME_BG, font=font)
    place(frame, left, top, width, height)
    return frame
