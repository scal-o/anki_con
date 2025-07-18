import json
import logging
import sys

import numpy as np
import pandas as pd


from ankicli import parseModule
from ankicli.anki_api import deckModule
from ankicli.anki_api.requestModule import request_action
from ankicli.renderer.img_plugin import im_list
from ankicli.renderer.rendererModule import markdown

# set up logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)

debug_handler = logging.StreamHandler(stream=sys.stdout)
debug_handler.setLevel(logging.DEBUG)

formatter = logging.Formatter("%(name)s::%(levelname)s - %(message)s")
debug_handler.setFormatter(formatter)

logger.addHandler(debug_handler)


class NoteSet:
    def __init__(self):
        self.deckName = None
        self.tags = None
        self.file_path = None
        self.media = None
        self.df = None

    @classmethod
    def from_file(cls, path: str):
        """Method to instantiate a NoteSet object from a text file"""

        logger.info(f"Instantiating NoteSet from file: {path}")

        # create class instance
        nset = cls()

        # save file path info
        nset.file_path = path

        # retrieve file lines and properties
        logger.debug("Reading file lines")
        lines = parseModule.get_lines(path)
        properties, lines = parseModule.extract_properties(lines)

        # assign deckName and common tags as found in the yaml frontmatter (properties)
        logger.debug("Parsing deck and tags info from file yaml frontmatter")
        metadata = parseModule.get_properties_metadata(properties)

        nset.deckName = parseModule.get_deck(metadata)
        nset.tags = parseModule.get_tags(metadata)

        # group file lines and create pandas.Series and pandas.DataFrame
        grouped_lines = parseModule.group_lines(lines)
        grouped_lines = pd.Series(grouped_lines, name="text")
        grouped_lines = pd.DataFrame(grouped_lines)

        # create and fill pandas.DataFrame with the card information
        logger.debug("Parsing cards from file lines")
        df = grouped_lines.text.apply(parseModule.parse_card)
        df = pd.concat([grouped_lines, df], axis=1)

        # add properties to the DataFrame
        properties = pd.DataFrame({'text': [properties]})
        properties = pd.concat([properties, properties.text.apply(parseModule.parse_card, return_empty=True)], axis=1)

        df = pd.concat([properties, df], ignore_index=True)

        # format front and back of cards
        logger.debug("Formatting front and back text")
        df[["front", "back"]] = df[["front", "back"]].map(markdown)

        # add media
        logger.debug("Scraping images from file lines")
        nset.media = im_list.copy()

        # create field column
        logger.debug("Creating fields column")
        df.loc[df["is_card"], "fields"] = df.apply(
            lambda x: {"Front": x.front, "Back": x.back}, axis=1
        )

        # add tags and deck info to cards
        logger.debug("Adding tags and deck info")
        df["tags"] = np.empty((len(df.index), 0)).tolist()
        df.tags.apply(lambda x: x.extend(nset.tags))
        df["deckName"] = nset.deckName

        # # save cards df
        nset.df = df

        # return instantiated NoteSet
        logger.info("NoteSet instantiated")
        return nset

    def check_deck(self) -> None:
        """Method to check that the NoteSet deck exists in the server and create it if it does not."""

        logger.debug("Checking deck")
        if not deckModule.deck_exists(self.deckName):
            deckModule.create_deck(self.deckName)

    @staticmethod
    def add_notes(df: pd.DataFrame) -> list:
        """Method to add notes to anki"""

        # creating list of cards to add
        logger.debug("Creating list of cards to upload")
        df = df[["deckName", "modelName", "fields", "tags"]].copy()
        df_l = df.to_dict(orient="index")
        df_l = list(df_l.values())

        # uploading cards
        logger.debug("Uploading cards to anki server")
        result = request_action("addNotes", notes=df_l)["result"]

        return result

    def check_notes(self) -> None:
        """Method to perform general checks on the database for different kind of notes that could build up to errors:
        - deleted notes
        - duplicate notes
        - wrong deck notes"""

        logger.info("Checking notes")

        # # # checks on new notes =====
        # check on new notes first as duplicate notes might have to be updated, while deleted notes only need to be
        # uploaded to anki again
        logger.debug("Checking new notes")

        # copy original dataframe
        df = self.df.copy()

        # filter dataframe to only keep cards that don't have an id
        df = df.loc[(df["is_card"] == True) & (df["id"].isna())]

        # find and repair error notes
        logger.debug("Find and repair errors in new notes")
        df, e_df = self.repair_errors(df)

        # if some notes could not be added, remove them from the df and write an error log
        if len(e_df) != 0:
            logging.warning("\nSome of the new notes could not be added to the deck.")
            self.write_to_error_log(e_df)
            df = df.loc[~df.index.isin(e_df.index)]

        # update df with duplicate notes info (id, etc.)
        self.df.update(df)
        # remove error notes from the noteset df
        # TODO replace deleting notes with maybe setting is_card to False in order not to upload them and return errors
        self.df = self.df.loc[~self.df.index.isin(e_df.index)]

        # # # checks on existing notes =====
        # check on existing notes to find notes that have been deleted from the server but still have an id in the
        # file
        logger.debug("Checking existing notes")

        # copy original dataframe
        df = self.df.copy()

        # filter dataframe to only keep cards that already have an id
        df = df.loc[(df["is_card"] == True) & (~df["id"].isna())]

        # gather ids of existing notes
        df_ids = df["id"].map(int)
        df_ids = df_ids.to_list()

        # query the database
        logger.debug("Querying anki for notes info")
        queried_notes = request_action("notesInfo", notes=df_ids)["result"]
        queried_notes = [note if len(note) != 0 else None for note in queried_notes]

        logger.debug("Finding and repairing deleted notes")
        # separate deleted notes
        df, deleted_notes = self.find_deleted_notes(df, queried_notes)

        # repair deleted notes if there are any
        if not deleted_notes.empty:
            deleted_notes = self.repair_deleted_notes(deleted_notes)
            self.df.loc[deleted_notes.index] = deleted_notes

        # check and adjust deck for the existing notes
        logger.debug("Adjust deck of already existing cards")
        self.adjust_notes_deck(df)

    def update_existing_notes(self) -> None:
        """Method to update already existing notes to the anki server"""

        logger.info("Updating existing notes")

        # copy original dataframe
        df = self.df.copy()

        # filter dataframe to only keep cards that already have an id
        df = df.loc[(df["is_card"] == True) & (~df["id"].isna())]

        if not df.empty:
            # gather ids of existing notes
            df_ids = df["id"].map(int)
            df_ids = df_ids.to_list()

            # query the database
            logger.debug("Querying anki for existing notes")
            queried_notes = request_action("notesInfo", notes=df_ids)["result"]
            queried_notes = [note if len(note) != 0 else None for note in queried_notes]

            # divide existing notes in various dfs
            logger.debug("Finding notes that have to be updated")
            updatable_notes, non_updatable_notes = self.find_updatable_notes(
                df, queried_notes
            )

            # create list of notes from df
            logger.debug("Creating list of updatable notes")
            nl = updatable_notes[
                ["deckName", "modelName", "fields", "tags", "id"]
            ].copy()
            nl.id = nl.id.map(int)
            nl = nl.to_dict(orient="index")
            nl = list(nl.values())

            # update notes
            logger.debug("Updating notes")
            for note in nl:
                request_action("updateNote", note=note)

    def upload_new_notes(self) -> None:
        """Method to upload new notes to the anki server"""

        logger.info("Uploading new notes")

        # copy original dataframe
        df = self.df.copy()

        # filter dataframe to only keep cards that don't have an id
        df = df.loc[(df["is_card"] == True) & (df["id"].isna())]

        # if the df is empty, it means that no more cards have to be added to anki
        if not df.empty:
            # adding cards to the anki server
            logger.debug("Adding cards to server")
            df_ids = self.add_notes(df)

            # add ids to the
            logger.debug("Inserting ids into the cards' text")
            df["id"] = df_ids
            df["text"] = df.apply(parseModule.insert_card_id, axis=1)

            self.df.update(df)

    def repair_errors(self, df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Method to find and repair possible errors that may arise when uploading cards to Anki."""

        # copy df
        df = df.copy()

        # find out which notes may produce an error
        logger.debug("Finding error notes")
        e_df = self.find_error_notes(df)

        # repair any duplicated notes
        logger.debug("Repairing duplicate notes")
        dup_df = self.repair_duplicate_notes(e_df)

        # update repaired notes in the df
        df.update(dup_df)

        # drop repaired notes from the error df
        # kinda complex to do an anti-join tbh
        e_df = e_df.loc[~e_df.index.isin(dup_df.index)]

        return df, e_df

    @staticmethod
    def find_error_notes(e_df: pd.DataFrame) -> pd.DataFrame:
        """Method to check that all the new notes can be added to the deck."""

        # copy df
        e_df = e_df.copy()

        # create query for the anki server
        logger.debug("Creating anki query")
        e_ddf = e_df[["deckName", "modelName", "fields", "tags"]].copy()
        e_list = e_ddf.to_dict(orient="index")
        e_list = list(e_list.values())

        # launch queries and gather results
        logger.debug("Querying anki for possible errors in the cards")
        e_list = request_action("canAddNotesWithErrorDetail", notes=e_list)["result"]

        logger.debug("Extracting error cards")
        e_df["error"] = [el.get("error") for el in e_list]
        e_df.dropna(subset=["error"], inplace=True)

        return e_df

    @staticmethod
    def repair_duplicate_notes(e_df: pd.DataFrame) -> pd.DataFrame:
        """Method to repair eventual duplicate notes"""

        # filter error notes keeping the duplicate ones
        logger.debug("Filtering duplicate notes from other errors")
        dup_df = e_df.loc[
            e_df["error"] == "cannot create note because it is a duplicate"
        ].copy()

        # return the empty dataframe if no duplicate notes were found
        if dup_df.empty:
            return dup_df

        # gather front of the cards and use them as queries to retrieve card ids from anki
        logger.debug("Creating anki query")
        dup_front = dup_df["front"].to_list()

        # launch query and gather results
        logger.debug("Querying anki for the missing ids")
        dup_ids = [
            request_action("findNotes", query=el)["result"][0] for el in dup_front
        ]

        # insert card id in the id column and add it/sub it in the text column
        logger.debug("Inserting ids into the cards' text")
        dup_df["id"] = dup_ids
        dup_df["text"] = dup_df.apply(parseModule.insert_card_id, axis=1)

        # return the dataframe with the repaired cards
        return dup_df.drop(["error"], axis=1)

    @staticmethod
    def find_deleted_notes(df: pd.DataFrame, queried_notes: list) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Method to check that all the notes with an id actually exist in the anki server"""

        # copy dataframe
        df = df.copy()

        # filter deleted notes
        logger.debug("Filtering deleted notes")
        
        # use pandas isnull to find notes that have been deleted
        qn = pd.Series(queried_notes, index=df.index)
        deleted_notes = df[qn.isnull()].copy()
        df = df[qn.notnull()].copy()

        return df, deleted_notes

    @staticmethod
    def repair_deleted_notes(deleted_notes: pd.DataFrame) -> pd.DataFrame:
        """Method to repair deleted notes"""

        # copy original dataframe
        deleted_notes = deleted_notes.copy()

        # assign the id value to NaN
        deleted_notes.id = np.nan

        # delete id from card text
        logger.debug("Deleting ids from deleted cards")
        deleted_notes.text = deleted_notes.apply(parseModule.insert_card_id, axis=1)

        return deleted_notes

    @staticmethod
    def find_updatable_notes(df: pd.DataFrame, queried_notes: list):
        """Method to sort updatable and up-to-date notes"""

        # copy dataframe
        df = df.copy()

        # create list with important query info
        logger.debug("Building note fields from query result")
        queried_fields = [
            {
                "Front": x["fields"]["Front"]["value"],
                "Back": x["fields"]["Back"]["value"],
            }
            for x in queried_notes
            if x is not None
        ]

        # transform queried_fields into a pandas Series with the same index as df
        queried_fields = pd.Series(queried_fields, index=df.index)

        # create updatable / up to date notes dfs
        logger.debug("Divide notes in up-to-date and updatable")
        updatable_notes = df.loc[df.fields != queried_fields].copy()
        up_to_date_notes = df.loc[df.fields == queried_fields].copy()

        return updatable_notes, up_to_date_notes

    @staticmethod
    def adjust_notes_deck(df: pd.DataFrame) -> None:
        """Check that the notes belong to the right deck in anki"""

        # copy dataframe
        df = df.copy()

        if not df.empty:
            # retrieve ids and deckName
            df_ids = df.id.to_list()
            deck_name = df.deckName.iloc[0]

            # query the database to get a dictionary: {deck: [note ids]}
            logger.debug("Querying anki for card decks")
            deck_dict = request_action("getDecks", cards=df_ids)["result"]

            # gather ids of cards that are in the wrong deck
            logger.debug("Create list of cards in the wrong decks")
            wrong_deck_ids = []
            # for every key in the dictionary that is different from the one defined in the noteSet deckName attribute,
            # add the items of its list to the wrongDeck list
            for key in list(deck_dict):
                if key != deck_name:
                    wrong_deck_ids.extend(deck_dict[key])

            # change notes' deck
            logger.debug("Change cards deck")
            request_action("changeDeck", cards=wrong_deck_ids, deck=deck_name)

    @staticmethod
    def write_to_error_log(e_df: pd.DataFrame, file="error_log.txt") -> None:
        logger.warning("Writing error log...")

        # write error log with the notes in the error df
        with open(file, "a") as f:
            f.writelines(f"\n{json.dumps(e_df.to_dict(orient='index'))}")

    def upload_media(self) -> None:
        """Method to upload media to anki server"""

        # upload every image to the media folder
        for file in self.media:
            request_action(
                "storeMediaFile", filename=file["filename"], path=str(file["path"].absolute())
            )

    def save_file(self) -> None:
        """Method to save the updated lines to the file"""

        # create file lines list
        lines = self.df.text.to_list()
        lines = [line for group in lines for line in group]

        # write lines to file
        with open(self.file_path, mode="w", encoding="utf-8") as f:
            f.writelines(lines)
