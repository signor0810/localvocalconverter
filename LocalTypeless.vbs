Set objFSO = CreateObject("Scripting.FileSystemObject")
strScriptPath = WScript.ScriptFullName
strScriptDir = objFSO.GetParentFolderName(strScriptPath)

Set objShell = CreateObject("WScript.Shell")
objShell.CurrentDirectory = strScriptDir

strPythonw = objFSO.BuildPath(strScriptDir, ".venv\Scripts\pythonw.exe")
strCommand = """" & strPythonw & """ -m local_typeless.ui"

' 0 = Hide window (run completely in background with no console)
objShell.Run strCommand, 0, False
