import re
from prompt_toolkit import prompt
from prompt_toolkit.completion import WordCompleter

def single_select_input(requestMsg: str, selections: list[str], emptyMsg: str = None, as_list: bool = True):
    if not requestMsg or not selections:
        raise ValueError("Missing arguments.")
    elif len(selections) == 1:
        return selections[0]
    
    input_str = ""
    if as_list:
        for index, item in enumerate(selections):
            input_str += f"    {index}) - {item}\n"
    else:
        input_str = " | ".join(selections) + '\n'
    input_str += f"{requestMsg} {f"or leave blank({emptyMsg})" if emptyMsg else ""}: "

    completer = WordCompleter(selections, ignore_case=True)
    user_input = prompt(input_str, completer=completer).strip()

    if not user_input:
        if emptyMsg == None:
            raise ValueError("Didn't provide selection.")
        return None
    
    if user_input.isdigit():
        numeric_selection = int(user_input)
        if numeric_selection >= len(selections):
            raise ValueError(f"{numeric_selection} is not a valid numeric selection.")
        return selections[numeric_selection]
    else:
        if user_input not in selections:
            raise ValueError(f"{user_input} is not a valid selection.")
        return user_input

def multi_select_input(requestMsg: str, selections: list[str], emptyMsg: str = None):
    if not requestMsg or not selections:
        raise ValueError("Missing arguments.")
    if len(selections) == 1:
        return selections
    
    remaining_items = selections[:]
    selected_items = []

    while remaining_items:
        input_str = (f"Selected: {" | ".join(selected_items)}\n" if selected_items else "") + requestMsg
        user_input = single_select_input(input_str, [*remaining_items, "ALL"], "", False)

        if not user_input:
            if not emptyMsg and not selected_items:
                raise ValueError(f"Did not provide selections.")
            return selected_items
        elif user_input == "ALL":
            return selections
        
        remaining_items.remove(user_input)
        selected_items.append(user_input)
    
    return selected_items
    
def tuple_input(requestMsg: str, keys: list[str]):
    if not requestMsg or not keys:
        raise ValueError("Missing arguments.")
    if len(keys) == 1:
        return keys
    
    input_str = requestMsg + f"({','.join(keys)}) (comma-separeted):"
    user_input = input(input_str)

    items = re.findall(r"\(([^()]+)\)", user_input)
    formatted_items = []
    for item in items:
        values = item.strip("()").split(',')
        if len(values) != len(keys):
            raise ValueError("Invalid values passed")
        formatted_items.append(tuple([int(value) if value.isdigit() else value for value in values]))

    return formatted_items