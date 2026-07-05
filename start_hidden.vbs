' start_hidden.vbs — Launches the tracker with NO visible console window.
' Double-click to start. The browser does NOT auto-open.

Option Explicit

Dim shell, fso, scriptDir, wrapperPath
Set shell = CreateObject("WScript.Shell")
Set fso   = CreateObject("Scripting.FileSystemObject")

scriptDir   = fso.GetParentFolderName(WScript.ScriptFullName)
wrapperPath = scriptDir & "\start_hidden_wrapper.bat"

If Not fso.FileExists(wrapperPath) Then
    MsgBox "start_hidden_wrapper.bat not found in " & scriptDir & ".", vbCritical, "Tracker"
    WScript.Quit 1
End If

' WindowStyle 0 = hidden. The .bat runs detached and writes
' progress to data\tracker_stdout.log so we can diagnose later.
shell.Run """" & wrapperPath & """", 0, False

WScript.Sleep 1500
MsgBox "Jackpot Tracker launched in background." & vbCrLf & vbCrLf & _
       "Open http://127.0.0.1:5050 in your browser." & vbCrLf & vbCrLf & _
       "If the page doesn't load, check:" & vbCrLf & _
       "  data\tracker_stdout.log", _
       vbInformation, "Tracker"
