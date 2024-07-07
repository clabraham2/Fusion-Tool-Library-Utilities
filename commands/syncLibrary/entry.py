import adsk.core, adsk.fusion, adsk.cam, traceback
import os
from ...lib import fusion360utils as futil
from ... import config
from typing import List, Dict
from adsk.cam import ToolLibrary, Tool, DocumentToolLibrary

app = adsk.core.Application.get()
ui: adsk.core.UserInterface = app.userInterface

CMD_ID = f'{config.COMPANY_NAME}_{config.ADDIN_NAME}_Sync_Tools_with_Library'
CMD_NAME = 'Sync Tools with Library'
CMD_Description = 'Sync Tools with Library'
IS_PROMOTED = True

WORKSPACE_ID = 'CAMEnvironment'
PANEL_ID = 'CAMManagePanel'
COMMAND_BESIDE_ID = ''

ICON_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources', '')

# Local list of event handlers used to maintain a reference so
# they are not released and garbage collected.
local_handlers = []

def start():
    cmd_def = ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_Description, ICON_FOLDER)
    futil.add_handler(cmd_def.commandCreated, command_created)
    workspace = ui.workspaces.itemById(WORKSPACE_ID)
    panel = workspace.toolbarPanels.itemById(PANEL_ID)
    control = panel.controls.addCommand(cmd_def, COMMAND_BESIDE_ID, False)
    control.isPromoted = IS_PROMOTED

def stop():
    # Get the various UI elements for this command
    workspace = ui.workspaces.itemById(WORKSPACE_ID)
    panel = workspace.toolbarPanels.itemById(PANEL_ID)
    command_control = panel.controls.itemById(CMD_ID)
    command_definition = ui.commandDefinitions.itemById(CMD_ID)

    if command_control:
        command_control.deleteMe()

    if command_definition:
        command_definition.deleteMe()

def command_created(args: adsk.core.CommandCreatedEventArgs):
    # General logging for debug.
    futil.log(f'>>> {CMD_NAME} Command Created Event')
    futil.add_handler(args.command.execute, command_execute, local_handlers=local_handlers)
    futil.add_handler(args.command.destroy, command_destroy, local_handlers=local_handlers)

    inputs = args.command.commandInputs

    # Option to select which tooling library to use
    library_input = inputs.addDropDownCommandInput('library', 'Library', adsk.core.DropDownStyles.TextListDropDownStyle)
    # Get the list of tooling libraries
    libraries = get_tooling_libraries()
    # Format the list of libraries for display in the drop down
    library_input.tooltipDescription = 'Select the tool library you would like to replace from.'
    formatted_libraries = format_library_names(libraries)
    for library in formatted_libraries:
        library_input.listItems.add(library, True)
    # print them to the console for debug
    # futil.log(f'Available libraries: {libraries}')

    # Make a drop down for match type
    match_input = inputs.addDropDownCommandInput('match', 'Match Type', adsk.core.DropDownStyles.TextListDropDownStyle)
    match_input.listItems.add('Tool Number', True)
    match_input.listItems.add('Comment', False)
    match_input.listItems.add('Product ID', False)
    match_input.listItems.add('Description', False)
    match_input.listItems.add('Geometry', False)

    # Make a drop down for sync direction
    syncDirection_input = inputs.addDropDownCommandInput('syncDirection', 'Sync Direction', adsk.core.DropDownStyles.TextListDropDownStyle)
    syncDirection_input.listItems.add('Pull', True)
    syncDirection_input.listItems.add('Push', False)
    
    # Diff Only input
    diffOnly_input = inputs.addBoolValueInput('diffOnly_input', 'Log Differences Only ', True, '', False)


def command_execute(args: adsk.core.CommandEventArgs):
    # General logging for debug
    cam = adsk.cam.CAM.cast(app.activeProduct)
    inputs = args.command.commandInputs
    match_input: adsk.core.DropDownCommandInput = inputs.itemById('match')
    match_type = match_input.selectedItem.name
    syncDirection_input: adsk.core.DropDownCommandInput = inputs.itemById('syncDirection')
    syncDirection_type = syncDirection_input.selectedItem.name
    diffOnly_input: adsk.core.BoolValueInput = inputs.itemById('diffOnly_input')
    diffOnly_mode = diffOnly_input.value
    library_input: adsk.core.DropDownCommandInput = inputs.itemById('library')
    camManager = adsk.cam.CAMManager.get()
    libraryManager = camManager.libraryManager
    toolLibraries = libraryManager.toolLibraries
    libraries = get_tooling_libraries()
    formatted_libraries = format_library_names(libraries)
    library_index = formatted_libraries.index(library_input.selectedItem.name)
    library_url = adsk.core.URL.create(libraries[library_index])
    library = toolLibraries.toolLibraryAtURL(library_url)

    # set parameter name for input selection
    matchParameter = ''
    match match_type:
        case 'Comment':
            matchParameter = 'tool_comment'
        case 'Product ID':
            matchParameter = 'tool_productId'
        case 'Description':
            matchParameter = 'tool_description'
        case 'Tool Number':
            matchParameter = 'tool_number'

    # skip writing values if diffOnly_mode is true
    writeToTarget = True
    if diffOnly_mode == True:
        writeToTarget = False

    # reassign doucument tools and library tools to convenient names based on sync direction
    if syncDirection_type == 'Pull':
        sourceLibrary = library
        targetLibrary = cam.documentToolLibrary
    if syncDirection_type == 'Push':
        sourceLibrary = cam.documentToolLibrary
        targetLibrary = library

    # User verify that settings are correct
    buttonClicked = ui.messageBox(f'Synchronization will proceed with the following settings: \n\nMatch: {match_type} \nLibrary: {formatted_libraries[library_index]} \nDirection: {syncDirection_type} \nLog Differences Only: {diffOnly_mode} \n\nDue to API limitations, tool holder geometry cannot be updated.', "Verify Synchronization Settings.",1,2) #0 OK, -1 Error, 1 Cancel, 2 Yes or Retry, 3 No
    match buttonClicked:
        case 0:
            futil.log(f'Match: {match_type}\n Library: {formatted_libraries[library_index]}\n Direction: {syncDirection_type}\n Log Differences Only: {diffOnly_mode}')
            pass
        case 1:
            return
    
    # Check if the source library has multiple instances of the match parameter. The command will not continue until the collisions are resolved.
    if hasCollisions(matchParameter, sourceLibrary):
        ui.messageBox(f'Multiple tool instances with the same \'{match_type}\' were found in \'{formatted_libraries[library_index]}\'. There may only be one instance of each match before synchronization will continue. See log for details.')
        return

    for targetTool in targetLibrary:
        matchValue = targetTool.parameters.itemByName(matchParameter).value.value # convenient to have as a shorter variable name

        sourceTool = [item for item in sourceLibrary if item.parameters.itemByName(matchParameter).value.value == matchValue][0] # Find SOURCE tool by parameter name, b/c iterating over target tools. Duplicates should be caught by hasCollisions()

        # Step 1/3 - Parameters
        for toolParameter in sourceTool.parameters:
            try: 
                writeDiffToLog(matchValue, toolParameter.name, targetTool.parameters.itemByName(toolParameter.name).value.value, sourceTool.parameters.itemByName(toolParameter.name).value.value)
                if writeToTarget:
                    targetTool.parameters.itemByName(toolParameter.name).value.value = sourceTool.parameters.itemByName(toolParameter.name).value.value
            except Exception as error:
                # futil.log(error) # debug mode?
                futil.log('Failed to set \'' + toolParameter.name + '\' for ' + str(matchValue) + ' to ' + str(sourceTool.parameters.itemByName(toolParameter.name).value.value))
                pass

        # Step 2/3 - Presets
        for sourceToolPreset in sourceTool.presets:
            if not targetTool.presets.itemsByName(sourceToolPreset.name): # Add absent preset to target tool
                newPreset = targetTool.presets.add()
                newPreset.name = sourceToolPreset.name
                for parameter in sourceToolPreset.parameters:
                    newPreset.parameters.itemByName(parameter.name).value.value = sourceToolPreset.parameters.itemByName(parameter.name).value.value
                futil.log('Preset \'' + sourceToolPreset.name + '\' added to ' + str(matchValue))
            else: # Overwrite existing preset
                targetToolPreset = [item for item in targetTool.presets if item.name == sourceToolPreset.name][0] # Find TARGET tool preset by name, b/c interating over source tool presets from the tool that was found earlier. UI disallows same names, so there should not be duplciates
                for parameter in sourceToolPreset.parameters:
                    try:
                        writeDiffToLog(matchValue, str(sourceToolPreset.name + '\',\'' + parameter.name), targetToolPreset.parameters.itemByName(parameter.name).value.value, sourceToolPreset.parameters.itemByName(parameter.name).value.value)
                        if writeToTarget:
                            targetToolPreset.parameters.itemByName(parameter.name).value.value = sourceToolPreset.parameters.itemByName(parameter.name).value.value
                    except:
                        # futil.log(error) # debug mode?
                        futil.log('Failed to set ' + str(sourceToolPreset.name + ' ' + parameter.name) + ' for ' + str(matchValue) + ' to ' + str(sourceToolPreset.parameters.itemByName(parameter.name).value.value))
                        pass

        # Step 3/3 Holder - API does not currently support editing the holder geometry
        
        if syncDirection_type == 'Pull': #update tools in doc one at a time when pulling
            cam.documentToolLibrary.update(targetTool, True)

    if syncDirection_type == 'Push': #update library all at once at end when pushing
        toolLibraries.updateToolLibrary(library_url, library)

    ui.messageBox('Synchronization completed. See log for details')

# This event handler is called when the command terminates.
def command_destroy(args: adsk.core.CommandEventArgs):
    global local_handlers
    local_handlers = []
    futil.log(f'>>> {CMD_NAME} Command Destroy Event')

def hasCollisions(parameterName, library):
    valueList = []
    counter = {}
    for tool in library:
        valueList.append(tool.parameters.itemByName(parameterName).value.value)
    for value in valueList:
        counter[value] = counter.get(value, 0) + 1
    for value in list(counter.values()):
        if value > 1: # at least one parameter has more than one match
            futil.log(f'The following values for \'{parameterName}\' exist in more than one tool instance: <<<<<<<<<<')
            for key, value in counter.items(): #iterate over all keys to log all that have more than one match
                if value > 1:
                    futil.log(str(key))
            futil.log(f'Reduce to one instance of each and retry synchronization')
            return True
    return False

def writeDiffToLog(id, parameterName, targetValue, sourceValue):
    try: # float errors make logging diffs sensitive
        sourceValue = round(sourceValue,4)
        targetValue = round(targetValue,4)
    except:
        pass # value isn't a number
    if str(targetValue) != str(sourceValue):
        futil.log(str(id) + ' \'' + str(parameterName) + '\' ' + str(targetValue) + ' -> ' + str(sourceValue))
    return

def get_tooling_libraries() -> List:
    # Get the list of tooling libraries
    camManager = adsk.cam.CAMManager.get()
    libraryManager = camManager.libraryManager
    toolLibraries = libraryManager.toolLibraries
    fusion360Folder = toolLibraries.urlByLocation(adsk.cam.LibraryLocations.CloudLibraryLocation)
    libraries = getLibrariesURLs(toolLibraries, fusion360Folder)
    fusion360Folder = toolLibraries.urlByLocation(adsk.cam.LibraryLocations.LocalLibraryLocation)
    libraries = libraries + getLibrariesURLs(toolLibraries, fusion360Folder)
    fusion360Folder = toolLibraries.urlByLocation(adsk.cam.LibraryLocations.ExternalLibraryLocation)
    libraries = libraries + getLibrariesURLs(toolLibraries, fusion360Folder)
    return libraries

def getLibrariesURLs(libraries: adsk.cam.ToolLibraries, url: adsk.core.URL):
    ''' Return the list of libraries URL in the specified library '''
    urls: list[str] = []
    libs = libraries.childAssetURLs(url)
    for lib in libs:
        urls.append(lib.toString())
    for folder in libraries.childFolderURLs(url):
        urls = urls + getLibrariesURLs(libraries, folder)
    return urls

def format_library_names(libraries: List) -> List:
    # Format the list of libraries for display in the drop down
    formatted_libraries = []
    for library in libraries:
        formatted_libraries.append(library.split('/')[-1])
    return formatted_libraries