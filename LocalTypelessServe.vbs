Set objFSO = CreateObject("Scripting.FileSystemObject")
strScriptPath = WScript.ScriptFullName
strScriptDir = objFSO.GetParentFolderName(strScriptPath)

Set objShell = CreateObject("WScript.Shell")
objShell.CurrentDirectory = strScriptDir

strPythonw = objFSO.BuildPath(strScriptDir, ".venv\Scripts\pythonw.exe")
If Not objFSO.FileExists(strPythonw) Then
    strPythonw = "pythonw.exe"
End If
strCommand = """" & strPythonw & """ -m local_typeless.cli serve"

' 0 = Hide window (run completely in background with no console)
objShell.Run strCommand, 0, False
