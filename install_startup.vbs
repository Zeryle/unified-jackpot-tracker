' install_startup.vbs — Installs a Windows shortcut in shell:startup
' so the tracker starts automatically when you log in.
' Run ONCE. To uninstall, run uninstall_startup.vbs.

Option Explicit

Dim shell, fso, scriptDir, startupDir, shortcutPath
Set shell = CreateObject("WScript.Shell")
Set fso   = CreateObject("Scripting.FileSystemObject")

scriptDir    = fso.GetParentFolderName(WScript.ScriptFullName)
startupDir   = shell.SpecialFolders("Startup")
shortcutPath = startupDir & "\JackpotTracker.lnk"

' Use .lnk to the .vbs so double-click launches hidden AND auto-launch
' on startup uses the same hidden launcher.
Dim vbsPath
vbsPath = scriptDir & "\start_hidden.vbs"

If Not fso.FileExists(vbsPath) Then
    MsgBox "start_hidden.vbs not found in " & scriptDir & ".", vbCritical, "Tracker"
    WScript.Quit 1
End If

Dim link
Set link = shell.CreateShortcut(shortcutPath)
link.TargetPath       = "wscript.exe"
link.Arguments       = """" & vbsPath & """"
link.WorkingDirectory = scriptDir
link.WindowStyle      = 7  ' minimized
link.Description      = "Jackpot Tracker (hidden)"
link.IconLocation     = "shell32.dll,12"
link.Save

MsgBox "Auto-start installed:" & vbCrLf & shortcutPath & vbCrLf & vbCrLf & _
       "The tracker will launch in the background on every login.", _
       vbInformation, "Tracker"