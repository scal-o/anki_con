import re
import copy
"""Module to handle parsing of text files"""

# define a list of options used to compile the regular expressions throughout the module
precompiled = {
    "properties": "---",
    "deck": r"cards-deck: ([\w:\s]+)",
    "tags": r"^- ([\w/]+)",
    "question": r"^>\[!question]-?\s*(.+)(#card)",
    "answer": r"^>",
    "id": r"<!--ID: (\d+)-->|\^(\d+)",
    "empty_line": r"(\s+)?\n"
}

id_format = "^{}\n"


def get_lines(path):
    """Simple low-level function to read all lines from a file"""

    with open(path, mode="r", encoding="utf-8") as f:
        lines = list(f)
    return lines


def get_properties(lines: list) -> list:
    """Function that retrieves all the lines that make up the yaml properties frontmatter"""

    # compiles the regex expression before using it
    properties_re = re.compile(precompiled["properties"])

    # gets initial and final indices of the yaml frontmatter
    properties = [i for i, j in enumerate(lines) if properties_re.search(j) is not None]
    # gets properties text from file lines
    properties = [j.strip() for i, j in enumerate(lines) if i in range(properties[0], properties[1])]

    return properties


def get_deck(properties, deck_re="cards-deck") -> str:

    # compile the regex expression before using it
    deck_re = re.compile(precompiled["deck"])

    # extract the deck from the yaml frontmatter
    deck = [deck_re.search(line)[1] for line in properties if deck_re.search(line) is not None]

    # if no deck name is defined, return none (or maybe default with a warning?)
    if len(deck) == 0:
        return None
    else:
        return deck[0]

    return deck


def get_tags(properties) -> list:

    # compile the regex expression before using it
    tags_re = re.compile(precompiled["tags"])

    # extract the tags from the yaml frontmatter
    tags = [tags_re.search(line)[1].replace("/", "::") for line in properties if tags_re.search(line) is not None]

    return tags


def card_gen(lines, deck=None, tags=None):
    """Creates a generator object to iterate through the file lines and retrieve cards one by one, allowing the caller
    to modify the underlying lines list (for example by inserting the card id after uploading it)"""

    question_re = re.compile(precompiled["question"])
    answer_re = re.compile(precompiled["answer"])
    id_re = re.compile(precompiled["id"])
    empty_line_re = re.compile(precompiled["empty_line"])

    # create a standard dictionary to use as a template
    std_dict = {"Front": None, "Back": None, "id": None, "deckName": deck, "tags": tags}
    card_dict = copy.deepcopy(std_dict)

    for i, line in enumerate(lines):
        if question_re.search(line) is not None:
            card_dict["Front"] = question_re.search(line).group(1)
        elif answer_re.search(line) is not None:
            if card_dict["Back"] is None:
                card_dict["Back"] = line.strip(">")
            else:
                card_dict["Back"] += line.strip(">")
        elif id_re.search(line) is not None:
            card_dict["id"] = [int(group) for group in id_re.search(line).groups() if group is not None][0]
        elif empty_line_re.search(line) is not None:
            if card_dict["Front"] is not None and card_dict["Back"] is not None:
                card_dict["Back"] = card_dict["Back"].replace("\n", "<br />")
                yield card_dict, i
                card_dict = copy.deepcopy(std_dict)


def get_cards(lines, deck=None, tags=None) -> list[dict]:
    """Function to parse the whole file and return a list of dictionary where each dict represent a card"""

    question_re = re.compile(precompiled["question"])
    answer_re = re.compile(precompiled["answer"])
    id_re = re.compile(precompiled["id"])
    empty_line_re = re.compile(precompiled["empty_line"])

    # create an empty list and a standard dictionary to use as a template
    card_list = []
    std_dict = {"Front": None, "Back": None, "id": None, "deckName": deck, "tags": tags}
    card_dict = copy.deepcopy(std_dict)

    for line in lines:
        if question_re.search(line) is not None:
            card_dict["Front"] = question_re.search(line).group(1)
        elif answer_re.search(line) is not None:
            if card_dict["Back"] is None:
                card_dict["Back"] = line.strip(">")
            else:
                card_dict["Back"] += line.strip(">")
        elif id_re.search(line) is not None:
            card_dict["id"] = [group for group in id_re.search(line).groups() if group is not None][0]
        elif empty_line_re.search(line) is not None:
            if card_dict["Front"] is not None and card_dict["Back"] is not None:
                card_dict["Back"] = card_dict["Back"].replace("\n", "<br />")
                card_list.append(card_dict)
                card_dict = copy.deepcopy(std_dict)

    return card_list


def insert_card_id(lines, index, id) -> None:
    """Function to insert the card id in the lines of the file. Simple wrapper around .insert"""

    id_line = id_format.format(id)
    lines.insert(index, id_line)