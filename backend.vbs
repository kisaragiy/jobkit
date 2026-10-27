' jobkit backend launcher - starts ONLY the kernel backend (main.py serve), detached.
'
' Use start.vbs instead if you want the backend and desktop shell.
' This one is for: "the UI shows Failed to fetch and I just want the server back".
'
' Why detached matters: a backend started from a shell/agent session dies when
' that session is reaped. WScript.Shell.Run(cmd, style, False) is
' fire-and-forget and creates an independent process tree.
'
' !! NO HTTP PRE-CHECK IN THIS FILE !!
' The obvious design was to probe /api/overview first and skip if it answers.
' That was tried and it was WRONG: MSXML2.ServerXMLHTTP.6.0 combined with
' On Error Resume Next reported Status=200 even for a dead address
' (verified: Http200("http://127.0.0.1:59999/x") returned True). The launcher
' then always believed "already running" and NEVER started the backend while
' cheerfully reporting success. Do not re-add an HTTP probe here.
' Uniqueness is app.py's job: its port guard exits 3 without binding when
' another jobkit already owns 18770.
'
' !! KEEP THIS FILE PURE ASCII !!
' Windows Script Host reads .vbs as ANSI; a UTF-8 file containing CJK or
' box-drawing characters gets mis-decoded and the string literals break at
' parse time with a confusing "unterminated string constant" error.

Option Explicit

Dim sh, fso, base, py, cand, i, exe

Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)

Sub Say(msg, title)
  If LCase(Right(WScript.FullName, 11)) = "cscript.exe" Then
    WScript.Echo msg
  Else
    MsgBox msg, 64, title
  End If
End Sub

' Prefer a standalone CPython over whatever "python" resolves to on PATH:
' on this machine PATH puts the agent's own venv first, which would tie the
' backend to the agent and add an extra launcher process.
cand = Array( _
  sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python313\python.exe", _
  sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python312\python.exe", _
  sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python311\python.exe", _
  "C:\Python311\python.exe", "C:\Python312\python.exe" )
py = "python"
For i = 0 To UBound(cand)
  If fso.FileExists(cand(i)) Then
    py = cand(i)
    Exit For
  End If
Next

exe = base & "\main.py"
If Not fso.FileExists(exe) Then
  Say "main.py not found:" & vbCrLf & exe, "jobkit"
  WScript.Quit 1
End If

sh.CurrentDirectory = base
' style 0 = SW_HIDE (no console box). Launch the interpreter DIRECTLY:
' "cmd /c "quoted-program" arg" silently fails under cmd's quote
' disambiguation rules -- the child is never started and cmd reports no error.
sh.Run """" & py & """ """ & exe & """ serve", 0, False

WScript.Sleep 4000
Say "jobkit backend launch requested." & vbCrLf & vbCrLf & _
    "python : " & py & vbCrLf & _
    "port   : http://127.0.0.1:18770" & vbCrLf & vbCrLf & _
    "If it was already running, app.py's port guard rejected this duplicate" & vbCrLf & _
    "and nothing changed. Give it ~5s, then reload the UI page.", "jobkit"
WScript.Quit 0
