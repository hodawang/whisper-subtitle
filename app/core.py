"""Thin wrapper class to manage Media objects."""
import shutil
import time
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, List, Union

import ffmpeg
import numpy as np

import torch
import whisper
from config import MEDIA_DIR
from db import ENGINE, Media, Segment, Transcript
from pytube import Playlist, YouTube
from sqlalchemy.orm import Session

N_GPUS = torch.cuda.device_count()


def ratio(x):
    return (x[0])/x[1]
def check_gpu_ok(i):
    return ratio(torch.cuda.mem_get_info(i)) > 0.5
def return_gpu():
    for i in range(N_GPUS):
        if check_gpu_ok(i):
            return f'cuda:{i}'
            break
        elif i == N_GPUS-1:
            print('!!!No space left!!!')
            return f'cpu'

N_MODELS = 10
model_ok = []
for i in range(N_MODELS):
    # initial state flag
    model_ok.append(True)



# Whisper transcription functions
# ----------------
@lru_cache(maxsize=10)
def get_whisper_model(whisper_model: str, model_id: int):
    """Get a whisper model from the cache or download it if it doesn't exist"""
    model_ok[model_id] = False
    dev1 = return_gpu()
    model = whisper.load_model(whisper_model, device="cpu")
    
    model.encoder.to(dev1)
    dev2 = return_gpu()
    model.decoder.to(dev2)

    model.decoder.register_forward_pre_hook(lambda _, inputs: tuple([inputs[0].to(dev2), inputs[1].to(dev2)] + list(inputs[2:])))
    model.decoder.register_forward_hook(lambda _, inputs, outputs: outputs.to(dev1))
    return model


# Media manager class
# -------------------
class MediaManager:
    """A class to act as a primary interface to manage media objects and related data"""

    def __init__(self, media_dir: Path = MEDIA_DIR):
        self.media_dir = media_dir
        # Create the media directory if it doesn't exist
        self.media_dir.mkdir(exist_ok=True, parents=True)
        self.session = Session(ENGINE)

    def _transcribe(self, audio_path: str, whisper_model: str, **whisper_args):
        """Transcribe the audio file using whisper"""

        # Get whisper model
        # NOTE: If mulitple models are selected, this may keep all of them in memory depending on the cache size
        ok_flag = False
        while not ok_flag:
            for model_id in range(N_MODELS):
                if model_ok[model_id]:
                    transcriber = get_whisper_model(whisper_model, model_id)
                    ok_flag = True
                    break
            time.sleep(5)
            print('All models are busy')

        # Set configs & transcribe
        if whisper_args["temperature_increment_on_fallback"] is not None:
            whisper_args["temperature"] = tuple(
                np.arange(whisper_args["temperature"], 1.0 + 1e-6, whisper_args["temperature_increment_on_fallback"])
            )
        else:
            whisper_args["temperature"] = [whisper_args["temperature"]]

        del whisper_args["temperature_increment_on_fallback"]
        
        ok_flag = False
        while not ok_flag:
            try:
                transcript = transcriber.transcribe(
                    audio_path,
                    **whisper_args,
                )
                model_ok[model_id] = True
                ok_flag = True
            except:
                ok_flag = False

        return transcript

    def _transcribe_and_save(self, media_obj: Media, whisper_model: str, **whisper_args):
        """Transcribe the audio file using whisper and save the transcript to the database"""

        transcript = self._transcribe(media_obj.filepath, whisper_model, **whisper_args)

        # Write transcripts into the same directory as the audio file
        audio_dir = Path(media_obj.filepath).parent
        writer = whisper.utils.get_writer("all", audio_dir)
        writer(transcript, "transcript")

        # Add transcript to the database
        self.session.add(
            Transcript(
                media_id=media_obj.id,
                media=media_obj,
                text=transcript["text"],
                language=transcript["language"],
                generated_by=f"whisper-{whisper_model}",
            )
        )

        # Add all the segments to the database
        for segment in transcript["segments"]:
            self.session.add(
                Segment(
                    media_id=media_obj.id,
                    media=media_obj,
                    number=segment["id"],
                    text=segment["text"],
                    start=segment["start"],
                    end=segment["end"],
                    generated_by=f"whisper-{whisper_model}",
                    temperature=segment["temperature"],
                    avg_logprob=segment["avg_logprob"],
                    compression_ratio=segment["compression_ratio"],
                    no_speech_prob=segment["no_speech_prob"],
                )
            )
        self.session.commit()

    def _create(self, source_list: List[Union[str, Any]], source_type: str, whisper_model: str, **whisper_args):
        "Download audio files from the source and save it to the media directory"

        # Iterate over each source in the list & download the file
        for source in source_list:
            # If it is a youtube file, download it with pytube
            if source_type == "youtube":
                yc = YouTube(source)
                source_name = yc.title
                # itag = 140 is the audio only version
                # Check if directory already exists. If it does, append the current date and time to the directory name
                save_dir = self.media_dir / f"{source_name}"
                save_filename = "audio.mp4"
                if save_dir.exists():
                    save_dir = self.media_dir / f"""{source_name} - {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}"""
                # Download the audio file
                yc.streams.get_by_itag(140).download(save_dir, filename=save_filename)
            elif source_type == "upload":
                # Parse the file name from the source
                tokens = source.name.split(".")
                source_name = ".".join(tokens[:-1])
                source_format = tokens[-1]
                # Check if directory already exists. If it does, append the current date and time to the directory name
                save_dir = self.media_dir / f"{source_name}"
                if save_dir.exists():
                    save_dir = self.media_dir / f"""{source_name} - {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}"""
                # Create the directory
                save_dir.mkdir(exist_ok=True)
                save_filename = f"audio.{source_format}"
                # Save the audio file
                with open(save_dir / save_filename, "wb") as f:
                    f.write(source.read())

            # Save the media object to the database
            media_obj = Media(
                source_name=source_name,
                source_type=source_type,
                filepath=str(save_dir / save_filename),
            )
            # Add source link if it is a youtube file
            if source_type == "youtube":
                media_obj.source_link = source

            self.session.add(media_obj)

            # Transcribe the audio file
            self._transcribe_and_save(media_obj, whisper_model, **whisper_args)

        # Commit the changes to the database
        self.session.commit()

    def add(self, source: Union[str, Any], source_type: str, **whisper_args):
        "Adds a set of media objects to the database from YouTube or uploaded files"
        # If the source is a YouTube URL, expand it to a list of URLs
        if source_type == "youtube":
            # If the source is a playlist, download all the videos in the playlist
            if "list=" in source:
                source_list = list(Playlist(source))
            else:
                source_list = [source]
        elif source_type == "upload":
            source_list = source if type(source) == list else [source]

        # Download & save the audio files and add them to the database
        self._create(source_list=source_list, source_type=source_type, **whisper_args)

    def delete(self, media_id: str):
        "Delete a media object from the database"
        # Get the media object
        media_obj = self.session.query(Media).filter(Media.id == media_id).first()
        # Delete the media object
        self.session.delete(media_obj)
        # Commit the changes to the database
        self.session.commit()
        # Remove the audio file directory
        shutil.rmtree(Path(media_obj.filepath).parent)

    def get_segments(self, **filters):
        "Get all the segments in the database"

        filter_args = []

        if "start_date" in filters:
            filter_args.append(Segment.created >= filters["start_date"])
        if "end_date" in filters:
            # NOTE: Since dates are stored as timezone aware datetime strings, add 1 day to the end date
            # and filter until the end of that day
            end_date = datetime.strptime(filters["end_date"], "%Y-%m-%d")
            end_date = end_date + timedelta(days=1)
            end_date = end_date.strftime("%Y-%m-%d")
            filter_args.append(Segment.created < end_date)

        if "source_type" in filters:
            filter_args.append(Media.source_type == filters["source_type"])

        if "search_by_name" in filters:
            filter_args.append(Media.source_name.like(f"""%{filters["search_by_name"]}%"""))

        if "search_by_transcript" in filters:
            filter_args.append(Segment.text.like(f"""%{filters["search_by_transcript"]}%"""))

        # Get the media objects
        segment_objs = self.session.query(Segment).join(Media)

        # Filter the media objects
        if len(filter_args) > 0:
            segment_objs = segment_objs.filter(*filter_args)

        # Sort the media objects by creation date
        segment_objs = segment_objs.order_by(Media.created.desc()).limit(filters.get("limit", 10))

        # Finally, format media objects into a list of dictionaries
        formatted_segment_list = [self._format_segment(segment_obj) for segment_obj in segment_objs.all()]

        return formatted_segment_list

    def get_list(self, **filters):
        "List all the media objects in the database"

        # Get media objects & filter them as needed
        filter_args = []

        if "start_date" in filters:
            filter_args.append(Media.created >= filters["start_date"])
        if "end_date" in filters:
            # NOTE: Since dates are stored as timezone aware datetime strings, add 1 day to the end date
            # and filter until the end of that day
            end_date = datetime.strptime(filters["end_date"], "%Y-%m-%d")
            end_date = end_date + timedelta(days=1)
            end_date = end_date.strftime("%Y-%m-%d")
            filter_args.append(Media.created < end_date)

        if "source_type" in filters:
            filter_args.append(Media.source_type == filters["source_type"])

        if "search_by_name" in filters:
            filter_args.append(Media.source_name.like(f"""%{filters["search_by_name"]}%"""))

        if "search_by_transcript" in filters:
            filter_args.append(Transcript.text.like(f"""%{filters["search_by_transcript"]}%"""))

        # Get the media objects
        media_objs = self.session.query(Media).join(Transcript)

        # Filter the media objects
        if len(filter_args) > 0:
            media_objs = media_objs.filter(*filter_args)

        # Sort the media objects by creation date
        media_objs = media_objs.order_by(Media.created.desc()).limit(filters.get("limit", 10))

        # Finally, format media objects into a list of dictionaries
        formatted_media_list = [self._format_media_base(media_obj) for media_obj in media_objs.all()]

        return formatted_media_list

    def get_detail(self, media_id: str):
        "Get the details of a media object"
        media_obj = self.session.query(Media).filter(Media.id == media_id).first()
        return self._format_media_detail(media_obj)

    def _format_media_base(self, media_obj: Media):
        "Formats the media object to a dictionary"
        return {
            "id": media_obj.id,
            "source_name": media_obj.source_name,
            "source_type": media_obj.source_type,
            "source_link": media_obj.source_link,
            "filepath": media_obj.filepath,
            "created": media_obj.created,
            "language": media_obj.transcript.language,
            "generated_by": media_obj.transcript.generated_by,
        }

    def _format_media_detail(self, media_obj: Media):
        "Formats the media object to a dictionary"
        base = self._format_media_base(media_obj)
        details = {
            "transcript": media_obj.transcript.text,
            "segments": [
                {
                    "number": segment.number,
                    "start": segment.start,
                    "end": segment.end,
                    "text": segment.text,
                }
                for segment in media_obj.segments
            ],
        }
        base.update(details)
        return base

    def _format_segment(self, segment_obj: Segment):
        """Formats the segment object to a dictionary"""
        return {
            "id": segment_obj.id,
            "number": segment_obj.number,
            "start": segment_obj.start,
            "end": segment_obj.end,
            "text": segment_obj.text,
            "media": self._format_media_base(segment_obj.media),
        }
