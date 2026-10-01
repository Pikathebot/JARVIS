import logging
import os
import subprocess
from typing import Optional

try:
    from winotify import Notification, audio
    HAS_WINOTIFY = True
except ImportError:
    HAS_WINOTIFY = False

logger = logging.getLogger("jarvis.agent.tools.notify")


def _send_toast_powershell(title: str, message: str, urgent: bool = False) -> None:
    """Fallback toast notification using Windows PowerShell Windows.UI.Notifications."""
    escaped_title = title.replace('"', '`"').replace("'", "''")
    escaped_msg = message.replace('"', '`"').replace("'", "''")
    sound_xml = '<audio src="ms-winsoundevent:Notification.Looping.Alarm" loop="true"/>' if urgent else '<audio src="ms-winsoundevent:Notification.Default"/>'
    scenario_attr = 'scenario="alarm"' if urgent else ''

    ps_script = f"""
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
    $template = @"
    <toast {scenario_attr}>
        <visual>
            <binding template="ToastGeneric">
                <text>{escaped_title}</text>
                <text>{escaped_msg}</text>
            </binding>
        </visual>
        {sound_xml}
    </toast>
"@
    $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
    $xml.LoadXml($template)
    $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
    $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Jarvis Assistant")
    $notifier.Show($toast)
    """

    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
        capture_output=True,
        text=True,
        timeout=5
    )


def send_toast(title: str, message: str, urgent: bool = False) -> str:
    """
    Display a native Windows desktop toast notification alert.

    Args:
        title: Notification header title (e.g. 'Build Complete', 'Reminder', 'Alert').
        message: Notification body message content.
        urgent: If True, flags the notification as high-priority with alarm audio and urgent visual indication.
    """
    clean_title = (title or "Jarvis Notification").strip()
    clean_msg = (message or "").strip()

    if urgent and not clean_title.upper().startswith("[URGENT]"):
        clean_title = f"[URGENT] {clean_title}"

    displayed = False
    error_detail = ""

    if HAS_WINOTIFY:
        try:
            toast = Notification(
                app_id=os.environ.get("JARVIS_AUMID") or "Jarvis Assistant",
                title=clean_title,
                msg=clean_msg,
            )
            if urgent:
                try:
                    toast.set_audio(audio.Looping.Alarm, loop=False)
                except Exception:
                    pass

            toast.show()
            displayed = True
            logger.info("Sent toast notification via winotify: '%s'", clean_title)
        except Exception as we:
            logger.warning("winotify toast failed (%s), falling back to PowerShell", we)
            error_detail = str(we)

    if not displayed and os.name == "nt":
        try:
            _send_toast_powershell(clean_title, clean_msg, urgent=urgent)
            displayed = True
            logger.info("Sent toast notification via PowerShell fallback: '%s'", clean_title)
        except Exception as pe:
            logger.error("PowerShell toast fallback failed: %s", pe)
            error_detail = str(pe)

    if displayed:
        return f"Notification displayed: '{clean_title}' - '{clean_msg}'."
    else:
        return f"Error displaying desktop toast notification: {error_detail or 'Windows toast subsystem unavailable'}."
