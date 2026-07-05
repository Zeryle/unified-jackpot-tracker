' start_hidden.vbs — Launches the tracker with NO visible console window.
' Use this for "leave it running 8h unattended". All output that the
' app prints (logger + stdout) lands in data\tracker.log because
' pythonw detaches from any console. For crashes, see the
' data\tracker_stdout.log file written by this launcher.
'
' Double-click to start. The browser does NOT auto-open — open
' http://127.0.0.1:5050 manually.

Option Explicit

Dim shell, fso, scriptDir, logFile, vbsPath
Set shell = CreateObject("WScript.Shell")
Set fso   = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
vbsPath   = WScript.ScriptFullName

' Pick pythonw.exe from PATH. Prefer it (no console). Fall back to
' python.exe + redirected stdout so crashes still go to a file.
Dim pythonCmd
pythonCmd = ""

' shell.Exec + StdOut handles long paths properly. Try pythonw first.
On Error Resume Next
Dim exec1
Set exec1 = shell.Exec("where pythonw.exe")
Do While exec1.Status = 0 : WScript.Sleep 50 : Loop
If exec1.ExitCode = 0 Then
    Dim out1
    out1 = exec1.StdOut.ReadAll()
    Dim lines1 : lines1 = Split(Replace(Replace(out1, vbCr, ""), vbLf, ""), vbCrLf)
    If UBound(lines1) >= 0 And lines1(UBound(lines1)) <> "" Then
        pythonCmd = lines1(UBound(lines1))
    ElseIf UBound(lines1) >= 0 Then
        pythonCmd = lines1(0)
    End If
End If

If pythonCmd = "" Then
    Dim exec2
    Set exec2 = shell.Exec("where python.exe")
    Do While exec2.Status = 0 : WScript.Sleep 50 : Loop
    If exec2.ExitCode = 0 Then
        Dim out2 : out2 = exec2.StdOut.ReadAll()
        Dim lines2 : lines2 = Split(Replace(Replace(out2, vbCr, ""), vbLf, ""), vbCrLf)
        If UBound(lines2) >= 0 And lines2(UBound(lines2)) <> "" Then
            pythonCmd = lines2(UBound(lines2))
        ElseIf UBound(lines2) >= 0 Then
            pythonCmd = lines2(0)
        End If
    End If
End If
On Error Goto 0

If pythonCmd = "" Then
    MsgBox "Could not find pythonw.exe or python.exe in PATH.", vbCritical, "Tracker"
    WScript.Quit 1
End If

logFile = scriptDir & "\data\tracker_stdout.log"
If Not fso.FolderExists(scriptDir & "\data") Then
    fso.CreateFolder(scriptDir & "\data")
End If

' Build the launch command. With pythonw.exe: no console, no stdout
' (that's fine — the app writes to data\tracker.log). With python.exe:
' redirect stdout+stderr to a file so crashes leave a trail.
Dim finalCmd, usePythonw
usePythonw = (InStr(LCase(pythonCmd), "pythonw") > 0)

If usePythonw Then
    finalCmd = """" & pythonCmd & """ """ & scriptDir & "\app.py"""
Else
    finalCmd = """" & pythonCmd & """ """ & scriptDir & "\app.py"" > """ & logFile & """ 2>&1"
End If

' WindowStyle 0 = hidden. bWaitOnReturn False = fire-and-forget so
' we exit immediately and the python process keeps running.
shell.Run finalCmd, 0, False

WScript.Sleep 1000
MsgBox "Jackpot Tracker started in background." & vbCrLf & vbCrLf & _
       "Open http://127.0.0.1:5050 in your browser." & vbCrLf & _
       "Stop: use the UI's Stop button, or kill pythonw.exe in Task Manager.", _
       vbInformation, "Tracker"