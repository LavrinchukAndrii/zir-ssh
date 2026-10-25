# dmgbuild settings for zir.dmg, used by scripts/build.py:
#   python -m dmgbuild -s packaging/dmg_settings.py -D app=build/app-dist/zir.app -D packaging=packaging \
#       zir dist/zir.dmg
# Icon positions must match packaging/make_dmg_background.py.
import os.path

# dmgbuild execs this file without __file__, so paths come in as defines.
packaging = defines.get("packaging", "packaging")  # noqa: F821 (provided by dmgbuild)
app = defines["app"]  # noqa: F821
app_name = os.path.basename(app)

format = "UDZO"
filesystem = "HFS+"
files = [app]
symlinks = {"Applications": "/Applications"}
icon = os.path.join(packaging, "icons", "zir.icns")
hide_extensions = [app_name]

background = os.path.join(packaging, "dmg", "background.tiff")
window_rect = ((200, 120), (660, 440))
default_view = "icon-view"
show_toolbar = False
show_sidebar = False
show_status_bar = False
show_pathbar = False
show_tab_view = False
show_icon_preview = False
include_icon_view_settings = True
arrange_by = None
icon_size = 128
text_size = 13
icon_locations = {app_name: (170, 140), "Applications": (490, 140)}
