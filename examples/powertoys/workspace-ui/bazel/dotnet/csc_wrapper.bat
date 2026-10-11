@echo off
rem csc_assembly.bzl runs the compiler through this script:
rem
rem     csc_wrapper.bat <dotnet.exe> <csc.dll> @<parameter file>
rem
rem The compiler writes the absolute path of each source into the debug
rem information. The path of the directory an action runs in is different on
rem every machine and is not known when the command line is built, so it is
rem mapped away here (as rules_dotnet's own wrapper does).
rem
rem /noconfig (do not read the csc.rsp beside the compiler, which adds
rem references of its own) only counts on the command line itself.
setlocal
set DOTNET=%~1
set COMPILER=%~2
"%DOTNET%" "%COMPILER%" /noconfig -pathmap:"%CD%=." %3
exit /b %ERRORLEVEL%
