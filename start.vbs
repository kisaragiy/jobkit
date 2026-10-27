' jobkit launcher - canonical kernel backend and desktop, no console window.
'
' Why one file does everything: this must not depend on an agent/terminal session.
'   * a service started from a shell/agent session dies when that session is reaped
'     -> the UI suddenly shows "Failed to fetch"
'   * asking an agent to start it costs money/tokens every single time
' WScript.Shell.Run(cmd, style, False) is fire-and-forget and creates an
' independent process tree, so everything here survives closing the terminal.
'
' DESIGN: this script does NO "is it already running?" probing. It just launches
' both, because each of them is independently idempotent:
'   * main.py     -> port guard: port taken by jobkit? then exit 3, no duplicate
'   * Electron    -> requestSingleInstanceLock(): second launch quits immediately
'
' !! WHY NO HTTP PRE-CHECK IN VBS !!
' The obvious design was to poll http://127.0.0.1:18770/api/overview first and skip
' if it answers. That was tried and it was WRONG: MSXML2.ServerXMLHTTP.6.0 with
' On Error Resume Next reported Status=200 even for a dead address
' (verified: Http200("http://127.0.0.1:59999/x") returned True). Result: the
' launcher always believed "already running" and NEVER started the backend, while
' silently reporting success. Do not re-add a VBS HTTP probe -- let the children
' enforce their own uniqueness.
'
' What it brings up:
'   1) backend   main.py     127.0.0.1:18770   single kernel and local drafts
'   2) shell     Electron                    same-origin workspace
'
' Double-click to run. For console output (scriptable/testable): cscript start.vbs
'
' !! KEEP THIS FILE PURE ASCII !!
' Windows Script Host reads .vbs as ANSI. A UTF-8 file containing CJK or
' box-drawing characters gets mis-decoded, the character boundaries shift and
' swallow the following quotes, and you get a confusing
' "unterminated string constant" error that looks like a syntax mistake.

Option Explicit

Dim sh, fso, base, exe, wslpy, s

Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)

Sub Log(msg)
  If LCase(Right(WScript.FullName, 11)) = "cscript.exe" Then WScript.Echo msg
End Sub

Sub Say(msg)
  If LCase(Right(WScript.FullName, 11)) = "cscript.exe" Then
    WScript.Echo msg
  Else
    MsgBox msg, 64, "jobkit"
  End If
End Sub

' ============ 0) pick a python that does NOT belong to hermes ============
' Why: `python` on PATH resolves to hermes' own venv (verified), which means the
' backend would depend on hermes staying installed -- and its launcher shim also
' spawns a second process. A standalone CPython with fastapi/uvicorn/
' cryptography/pydantic already installed is used instead when present.
Function PickPython()
  Dim cand, i, p
  cand = Array( _
    sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python313\python.exe", _
    sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python312\python.exe", _
    sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python311\python.exe", _
    "C:\Python311\python.exe", "C:\Python312\python.exe" )
  For i = 0 To UBound(cand)
    If fso.FileExists(cand(i)) Then
      PickPython = cand(i)          ' BARE path, no quotes - caller quotes it
      Exit Function
    End If
  Next
  PickPython = "python"             ' fallback: whatever is on PATH
End Function

Dim py
py = PickPython()
Log "[0/3] python: " & py

' ============ 1) backend ============
' Fire-and-forget + SW_HIDE (no black console box).
' Duplicate launches are harmless: app.py's port guard exits 3 without binding.
Log "[1/3] backend  127.0.0.1:18770"
sh.CurrentDirectory = base
' Launch the interpreter DIRECTLY (no "cmd /c").
' !! Never use the form: cmd /c "quoted-program" args !!
' Windows cmd has its own quote-disambiguation rule when the first token is
' quoted; with that form the interpreter was never actually started and cmd
' reported no error either. Symptom: the script says "launched" but the backend
' never comes up. Running the executable directly has no such ambiguity.
sh.Run """" & py & """ """ & base & "\main.py"" serve", 0, False
WScript.Sleep 4000
Log "      launched (duplicate launch is rejected by the port guard)"

' Local draft tools require no background model process.

' ============ 3) Electron shell ============
Log "[3/3] Electron shell"
exe = base & "\shell\node_modules\electron\dist\electron.exe"

If Not fso.FileExists(exe) Then
  Say "Electron not found:" & vbCrLf & vbCrLf & exe & vbCrLf & vbCrLf & _
      "Run these in the shell folder:" & vbCrLf & _
      "  npm install --save-dev electron" & vbCrLf & _
      "  set ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/"
  WScript.Quit 1
End If

sh.CurrentDirectory = base & "\shell"
' window style 1 = SW_SHOWNORMAL.
' !! Must NOT be 0 (SW_HIDE) here !!
' 0 hides the launched process's main window. That is right for the console
' backend above, but Electron is a GUI app: with 0 the process starts and runs
' (several processes in task manager) yet NO window is ever shown, which looks
' exactly like "the app did not start". Verified with user32 EnumWindows.
sh.Run """" & exe & """ .", 1, False
Log "      launched (single-instance lock: an existing window is focused instead)"

' ============ summary ============
s = "jobkit" & vbCrLf & vbCrLf & _
    "backend : launching on http://127.0.0.1:18770" & vbCrLf & _
    "mode    : local draft workspace" & vbCrLf & _
    "shell   : launching" & vbCrLf & vbCrLf & _
    "If the UI shows 'Failed to fetch', wait ~5s and reload the page."
Log ""
Log s
Say s
