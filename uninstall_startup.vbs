' uninstall_startup.vbs — Removes the auto-start shortcut.
Option Explicit

Dim shell, fso, startupDir, shortcutPath
Set shell = CreateObject("WScript.Shell")
Set fso   = CreateObject("Scripting.FileSystemObject")

startupDir   = shell.SpecialFolders("Startup")
shortcutPath = startupDir & "\JackpotTracker.lnk"

If fso.FileExists(shortcutPath) Then
    fso.DeleteFile shortcutPath, True
    MsgBox "Auto-start removed: " & shortcutPath, vbInformation, "Tracker"
Else
    MsgBox "No auto-start shortcut found.", vbInformation, "Tracker"
End If