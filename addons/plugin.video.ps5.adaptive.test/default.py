import sys
from urllib.parse import parse_qs, urlencode

import xbmc
import xbmcgui
import xbmcplugin


MANIFEST = "https://dash.akamaized.net/akamai/bbb_30fps/bbb_30fps.mpd"
LABEL = "Big Buck Bunny - direct DASH (H.264 / AAC, no DRM)"


def main():
    handle = int(sys.argv[1])
    action = parse_qs(sys.argv[2].lstrip("?")).get("action", [""])[0]
    if action == "play":
        item = xbmcgui.ListItem(label=LABEL, path=MANIFEST)
        item.setProperty("inputstream", "inputstream.adaptive")
        item.setProperty("IsPlayable", "true")
        item.setMimeType("application/dash+xml")
        item.setContentLookup(False)
        xbmc.log(
            "[PS5AdaptiveTest] Resolving direct DASH via inputstream.adaptive: "
            + MANIFEST,
            xbmc.LOGINFO,
        )
        xbmcplugin.setResolvedUrl(handle, True, item)
    elif action == "":
        xbmcplugin.setContent(handle, "videos")
        item = xbmcgui.ListItem(label=LABEL)
        item.setProperty("IsPlayable", "true")
        item.getVideoInfoTag().setTitle(LABEL)
        url = sys.argv[0] + "?" + urlencode({"action": "play"})
        xbmcplugin.addDirectoryItem(handle, url, item, isFolder=False)
        xbmcplugin.endOfDirectory(handle)
    else:
        xbmc.log("[PS5AdaptiveTest] Invalid action: " + action, xbmc.LOGERROR)
        xbmcgui.Dialog().notification("PS5 Adaptive Playback Test", "Invalid action")
        xbmcplugin.endOfDirectory(handle, succeeded=False)


if __name__ == "__main__":
    main()
