
OmniDecompiler BETA – Quick Start Guide


1. Grab Python (One-time setup)

OmniDecompiler needs Python to run. Don't worry, it's completely free and takes a minute to set up.
1. Head over to https://www.python.org/downloads/ and hit that big Download Python button.
2. Open the installer. Crucial step: Before you click anything else, check the box at the bottom that says "Add python.exe to PATH". Then click Install Now.
Missed the checkbox? No sweat. If OmniDecompiler complains it can't find Python, just run the installer again, click Modify, and check the box to add Python to your environment variables.

2. Download OmniDecompiler

1. On the GitHub page, click the green Code button and choose Download ZIP.
2. Right-click the downloaded ZIP file, hit Extract All, and dump the folder somewhere handy like your Desktop.

3. Convert Your Maps

To get started, you just need a compiled map file (these end in .bsp). If you downloaded a map from the internet, it's probably already a loose .bsp file. Otherwise, games usually keep them right here:
• Half-Life: Steam\steamapps\common\Half-Life\valve\maps
• Counter-Strike 1.6: Steam\steamapps\common\Half-Life\cstrike\maps
• Half-Life 2: Steam\steamapps\common\Half-Life 2\hl2\maps
• CS: Source: Steam\steamapps\common\Counter-Strike Source\cstrike\maps
• Garry's Mod: Steam\steamapps\common\GarrysMod\garrysmod\maps
Note: Some games lock their maps inside .vpk files, so you won't see loose .bsp files lying around in those directories.
Once you have your map file, pick whichever method you like best:

Option A: The Double-Click Method

1. Double-click OmniDecompiler.bat inside your OmniDecompiler folder.
2. Select your map (or multiple maps) and hit Open.
3. A prompt will ask what format you want.
	• Choose No for .vmf format (if you use Source Hammer or Hammer++ for HL2, CS:S, GMod, etc.)
	• Choose Yes for .map format (if you use GoldSrc Hammer 3.5 or J.A.C.K. for HL1, CS 1.6, etc.)
4. Wait for the success message, and you're good to go!

Option B: The Drag & Drop Method

Simply grab your .bsp (or .map) files and drop them right on top of OmniDecompiler.bat. A window will pop up to show the progress. Once it's finished, just press any key to close it.