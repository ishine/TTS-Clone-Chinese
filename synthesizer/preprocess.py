from multiprocessing.pool import Pool 
from synthesizer import audio
from functools import partial
from itertools import chain
from encoder import inference as encoder
from pathlib import Path
from utils import logmmse
from tqdm import tqdm
import numpy as np
import librosa
import os
import platform

from pypinyin import lazy_pinyin, Style
from pypinyin.contrib.neutral_tone import NeutralToneWith5Mixin
from pypinyin.converter import DefaultConverter
from pypinyin.core import Pinyin

class MyConverter(NeutralToneWith5Mixin, DefaultConverter):
    pass

my_pinyin = Pinyin(MyConverter())
pinyin = my_pinyin.pinyin



def preprocess_librispeech(datasets_root: Path, out_dir: Path, n_processes: int, 
                           skip_existing: bool, hparams,dataset=None):
    # Gather the input directories
    dataset_root = datasets_root.joinpath("LibriSpeech")
    # input_dirs = [dataset_root.joinpath("train-clean-100"), 
    #               dataset_root.joinpath("train-clean-360")]
    input_dirs = [dataset_root.joinpath("train-clean-100")]
    print("\n    ".join(map(str, ["Using data from:"] + input_dirs)))
    assert all(input_dir.exists() for input_dir in input_dirs)
    
    # Create the output directories for each output file type
    out_dir.joinpath("mels").mkdir(exist_ok=True)
    out_dir.joinpath("audio").mkdir(exist_ok=True)
    
    # Create a metadata file
    metadata_fpath = out_dir.joinpath("train.txt")
    metadata_file = metadata_fpath.open("a" if skip_existing else "w", encoding="utf-8")

    # Preprocess the dataset
    speaker_dirs = list(chain.from_iterable(input_dir.glob("*") for input_dir in input_dirs))
    func = partial(preprocess_speaker, out_dir=out_dir, skip_existing=skip_existing, 
                   hparams=hparams)
    job = Pool(n_processes).imap(func, speaker_dirs)
    for speaker_metadata in tqdm(job, "LibriSpeech", len(speaker_dirs), unit="speakers"):
        for metadatum in speaker_metadata:
            metadata_file.write("|".join(str(x) for x in metadatum) + "\n")
    metadata_file.close()

    # Verify the contents of the metadata file
    with metadata_fpath.open("r", encoding="utf-8") as metadata_file:
        metadata = [line.split("|") for line in metadata_file]
    mel_frames = sum([int(m[4]) for m in metadata])
    timesteps = sum([int(m[3]) for m in metadata])
    sample_rate = hparams.sample_rate
    hours = (timesteps / sample_rate) / 3600
    print("The dataset consists of %d utterances, %d mel frames, %d audio timesteps (%.2f hours)." %
          (len(metadata), mel_frames, timesteps, hours))
    print("Max input length (text chars): %d" % max(len(m[5]) for m in metadata))
    print("Max mel frames length: %d" % max(int(m[4]) for m in metadata))
    print("Max audio timesteps length: %d" % max(int(m[3]) for m in metadata))



def preprocess_speaker(speaker_dir, out_dir: Path, skip_existing: bool, hparams):
    metadata = []
    for book_dir in speaker_dir.glob("*"):
        # Gather the utterance audios and texts
        try:
            alignments_fpath = next(book_dir.glob("*.alignment.txt"))
            with alignments_fpath.open("r") as alignments_file:
                alignments = [line.rstrip().split(" ") for line in alignments_file]
        except StopIteration:
            # A few alignment files will be missing
            continue
        
        # Iterate over each entry in the alignments file
        for wav_fname, words, end_times in alignments:
            wav_fpath = book_dir.joinpath(wav_fname + ".flac")
            assert wav_fpath.exists()
            words = words.replace("\"", "").split(",")
            end_times = list(map(float, end_times.replace("\"", "").split(",")))
            
            # Process each sub-utterance
            wavs, texts = split_on_silences(wav_fpath, words, end_times, hparams)
            for i, (wav, text) in enumerate(zip(wavs, texts)):
                sub_basename = "%s_%02d" % (wav_fname, i)
                metadata.append(process_utterance(wav, text, out_dir, sub_basename, 
                                                  skip_existing, hparams))
    
    return [m for m in metadata if m is not None]

def split_on_silences(wav_fpath, words, end_times, hparams):
    # Load the audio waveform
    wav, _ = librosa.load(wav_fpath, hparams.sample_rate)
    if hparams.rescale:
        wav = wav / np.abs(wav).max() * hparams.rescaling_max
    
    words = np.array(words)
    start_times = np.array([0.0] + end_times[:-1])
    end_times = np.array(end_times)
    assert len(words) == len(end_times) == len(start_times)
    assert words[0] == "" and words[-1] == ""
    
    # Find pauses that are too long
    mask = (words == "") & (end_times - start_times >= hparams.silence_min_duration_split)
    mask[0] = mask[-1] = True
    breaks = np.where(mask)[0]

    # Profile the noise from the silences and perform noise reduction on the waveform
    silence_times = [[start_times[i], end_times[i]] for i in breaks]
    silence_times = (np.array(silence_times) * hparams.sample_rate).astype(np.int)
    noisy_wav = np.concatenate([wav[stime[0]:stime[1]] for stime in silence_times])
    if len(noisy_wav) > hparams.sample_rate * 0.02:
        profile = logmmse.profile_noise(noisy_wav, hparams.sample_rate)
        wav = logmmse.denoise(wav, profile, eta=0)
    
    # Re-attach segments that are too short
    segments = list(zip(breaks[:-1], breaks[1:]))
    segment_durations = [start_times[end] - end_times[start] for start, end in segments]
    i = 0
    while i < len(segments) and len(segments) > 1:
        if segment_durations[i] < hparams.utterance_min_duration:
            # See if the segment can be re-attached with the right or the left segment
            left_duration = float("inf") if i == 0 else segment_durations[i - 1]
            right_duration = float("inf") if i == len(segments) - 1 else segment_durations[i + 1]
            joined_duration = segment_durations[i] + min(left_duration, right_duration)

            # Do not re-attach if it causes the joined utterance to be too long
            if joined_duration > hparams.hop_size * hparams.max_mel_frames / hparams.sample_rate:
                i += 1
                continue

            # Re-attach the segment with the neighbour of shortest duration
            j = i - 1 if left_duration <= right_duration else i
            segments[j] = (segments[j][0], segments[j + 1][1])
            segment_durations[j] = joined_duration
            del segments[j + 1], segment_durations[j + 1]
        else:
            i += 1
    
    # Split the utterance
    segment_times = [[end_times[start], start_times[end]] for start, end in segments]
    segment_times = (np.array(segment_times) * hparams.sample_rate).astype(np.int)
    wavs = [wav[segment_time[0]:segment_time[1]] for segment_time in segment_times]
    texts = [" ".join(words[start + 1:end]).replace("  ", " ") for start, end in segments]
    
    # # DEBUG: play the audio segments (run with -n=1)
    # import sounddevice as sd
    # if len(wavs) > 1:
    #     print("This sentence was split in %d segments:" % len(wavs))
    # else:
    #     print("There are no silences long enough for this sentence to be split:")
    # for wav, text in zip(wavs, texts):
    #     # Pad the waveform with 1 second of silence because sounddevice tends to cut them early
    #     # when playing them. You shouldn't need to do that in your parsers.
    #     wav = np.concatenate((wav, [0] * 16000))
    #     print("\t%s" % text)
    #     sd.play(wav, 16000, blocking=True)
    # print("")
    
    return wavs, texts
    
def process_utterance(wav: np.ndarray, text: str, out_dir: Path, basename: str, 
                      skip_existing: bool, hparams):
    ## FOR REFERENCE:
    # For you not to lose your head if you ever wish to change things here or implement your own
    # synthesizer.
    # - Both the audios and the mel spectrograms are saved as numpy arrays
    # - There is no processing done to the audios that will be saved to disk beyond volume  
    #   normalization (in split_on_silences)
    # - However, pre-emphasis is applied to the audios before computing the mel spectrogram. This
    #   is why we re-apply it on the audio on the side of the vocoder.
    # - Librosa pads the waveform before computing the mel spectrogram. Here, the waveform is saved
    #   without extra padding. This means that you won't have an exact relation between the length
    #   of the wav and of the mel spectrogram. See the vocoder data loader.
    
    
    # Skip existing utterances if needed
    mel_fpath = out_dir.joinpath("mels", "mel-%s.npy" % basename)
    wav_fpath = out_dir.joinpath("audio", "audio-%s.npy" % basename)
    if skip_existing and mel_fpath.exists() and wav_fpath.exists():
        return None
    
    # Skip utterances that are too short
    if len(wav) < hparams.utterance_min_duration * hparams.sample_rate:
        return None
    
    # Compute the mel spectrogram
    mel_spectrogram = audio.melspectrogram(wav, hparams).astype(np.float32)
    mel_frames = mel_spectrogram.shape[1]
    
    # Skip utterances that are too long
    if mel_frames > hparams.max_mel_frames and hparams.clip_mels_length:
        return None
    
    # Write the spectrogram, embed and audio to disk
    np.save(mel_fpath, mel_spectrogram.T, allow_pickle=False)
    np.save(wav_fpath, wav, allow_pickle=False)
    
    # Return a tuple describing this training example
    return wav_fpath.name, mel_fpath.name, "embed-%s.npy" % basename, len(wav), mel_frames, text
 
 
def embed_utterance(fpaths, encoder_model_fpath):
    if not encoder.is_loaded():
        encoder.load_model(encoder_model_fpath)

    # Compute the speaker embedding of the utterance
    wav_fpath, embed_fpath = fpaths
    wav = np.load(wav_fpath)
    wav = encoder.preprocess_wav(wav)
    embed = encoder.embed_utterance(wav)
    np.save(embed_fpath, embed, allow_pickle=False)
    
 
def create_embeddings(synthesizer_root: Path, encoder_model_fpath: Path, n_processes: int):
    wav_dir = synthesizer_root.joinpath("audio")
    metadata_fpath = synthesizer_root.joinpath("train.txt")
    assert wav_dir.exists() and metadata_fpath.exists()
    embed_dir = synthesizer_root.joinpath("embeds")
    embed_dir.mkdir(exist_ok=True)
    
    # Gather the input wave filepath and the target output embed filepath
    with metadata_fpath.open("r") as metadata_file:
        metadata = [line.split("|") for line in metadata_file]
        fpaths = [(wav_dir.joinpath(m[0]), embed_dir.joinpath(m[2])) for m in metadata]
        
    # TODO: improve on the multiprocessing, it's terrible. Disk I/O is the bottleneck here.
    # Embed the utterances in separate threads
    func = partial(embed_utterance, encoder_model_fpath=encoder_model_fpath)
    job = Pool(n_processes).imap(func, fpaths)
    list(tqdm(job, "Embedding", len(fpaths), unit="utterances"))


# thchs30
def preprocess_thchs30(datasets_root: Path, out_dir: Path, n_processes: int, 
                           skip_existing: bool, hparams,dataset=None):
    # Gather the input directories
    dataset_root = datasets_root.joinpath("data_thchs30")
    input_dirs = [dataset_root.joinpath("train")]
    print("\n    ".join(map(str, ["Using data from:"] + input_dirs)))
    assert all(input_dir.exists() for input_dir in input_dirs)
    
    # Create the output directories for each output file type
    out_dir.joinpath("mels").mkdir(exist_ok=True)
    out_dir.joinpath("audio").mkdir(exist_ok=True)
    
    # Create a metadata file
    metadata_fpath = out_dir.joinpath("train.txt")
    metadata_file = metadata_fpath.open("a" if skip_existing else "w", encoding="utf-8")

    # Preprocess the dataset
    speaker_dirs = list(chain.from_iterable(input_dir.glob("*.trn") for input_dir in input_dirs))
    func = partial(preprocess_speaker_thchs30, out_dir=out_dir, skip_existing=skip_existing, 
                   hparams=hparams)
    job = Pool(n_processes).imap(func, speaker_dirs)
    for speaker_metadata in tqdm(job, "thchs30", len(speaker_dirs), unit="speakers"):
        for metadatum in speaker_metadata:
            metadata_file.write("|".join(str(x) for x in metadatum) + "\n")
    metadata_file.close()

    # Verify the contents of the metadata file
    with metadata_fpath.open("r", encoding="utf-8") as metadata_file:
        metadata = [line.split("|") for line in metadata_file]
    mel_frames = sum([int(m[4]) for m in metadata])
    timesteps = sum([int(m[3]) for m in metadata])
    sample_rate = hparams.sample_rate
    hours = (timesteps / sample_rate) / 3600
    print("The dataset consists of %d utterances, %d mel frames, %d audio timesteps (%.2f hours)." %
          (len(metadata), mel_frames, timesteps, hours))
    print("Max input length (text chars): %d" % max(len(m[5]) for m in metadata))
    print("Max mel frames length: %d" % max(int(m[4]) for m in metadata))
    print("Max audio timesteps length: %d" % max(int(m[3]) for m in metadata))

def preprocess_speaker_thchs30(speaker_dir, out_dir: Path, skip_existing: bool, hparams):
    metadata = []

    # Gather the utterance audios and texts
    alignments_fpath = str(speaker_dir)
    alignments_fpath = alignments_fpath.replace("train", "data")
    with open(alignments_fpath,"rb") as alignments_file:
        alignments = [line for line in alignments_file.readlines()]
    
    # Iterate over each entry in the alignments file
    wav_fpath = alignments_fpath[:-4]
    words = alignments[1].decode().strip("\n")
    if platform.system() == "Windows":
        split = "\\"
    else:
        split = "/" 
    wav_fname = wav_fpath.split(split)[-1]
    assert os.path.exists(wav_fpath)
    # Process each sub-utterance
    wav, text = split_on_silences_thchs30(wav_fpath, words, hparams)
    sub_basename = "%s_%02d" % (wav_fname, 0)
    metadata.append(process_utterance(wav, text, out_dir, sub_basename, 
                                      skip_existing, hparams))
    
    return [m for m in metadata if m is not None]
    
def split_on_silences_thchs30(wav_fpath, words, hparams):
    # Load the audio waveform
    wav, _ = librosa.load(wav_fpath, hparams.sample_rate)
    wav = librosa.effects.trim(wav, top_db= 40, frame_length=2048, hop_length=512)[0]
    if hparams.rescale:
        wav = wav / np.abs(wav).max() * hparams.rescaling_max
    
    return wav, words

# data_aishell
def preprocess_data_aishell(datasets_root: Path, out_dir: Path, n_processes: int, 
                           skip_existing: bool, hparams,dataset=None):
    # Gather the input directories
    dataset_root = datasets_root.joinpath("data_aishell")
    # input_dirs = [dataset_root.joinpath("train-clean-100"), 
    #               dataset_root.joinpath("train-clean-360")]

    dict_info = {}
    transcript_dirs = dataset_root.joinpath("transcript/aishell_transcript_v0.8.txt")
    with open(transcript_dirs,"rb") as fp:
        dict_transcript = [v.decode() for v in fp]

    for v in dict_transcript:
        if not v:
            continue
        v = v.strip().replace("\n","").split(" ")
        dict_info[v[0]] = " ".join(v[1:])

    input_dirs = [dataset_root.joinpath("wav/train")]
    print("\n    ".join(map(str, ["Using data from:"] + input_dirs)))
    assert all(input_dir.exists() for input_dir in input_dirs)
    
    # Create the output directories for each output file type
    out_dir.joinpath("mels").mkdir(exist_ok=True)
    out_dir.joinpath("audio").mkdir(exist_ok=True)
    
    # Create a metadata file
    metadata_fpath = out_dir.joinpath("train.txt")
    metadata_file = metadata_fpath.open("a" if skip_existing else "w", encoding="utf-8")

    # Preprocess the dataset
    speaker_dirs = list(chain.from_iterable(input_dir.glob("*") for input_dir in input_dirs))
    func = partial(preprocess_speaker_data_aishell, out_dir=out_dir, skip_existing=skip_existing, 
                   hparams=hparams, dict_info=dict_info)
    job = Pool(n_processes).imap(func, speaker_dirs)
    for speaker_metadata in tqdm(job, "data_aishell", len(speaker_dirs), unit="speakers"):
        for metadatum in speaker_metadata:
            metadata_file.write("|".join(str(x) for x in metadatum) + "\n")
    metadata_file.close()

    # Verify the contents of the metadata file
    with metadata_fpath.open("r", encoding="utf-8") as metadata_file:
        metadata = [line.split("|") for line in metadata_file]
    mel_frames = sum([int(m[4]) for m in metadata])
    timesteps = sum([int(m[3]) for m in metadata])
    sample_rate = hparams.sample_rate
    hours = (timesteps / sample_rate) / 3600
    print("The dataset consists of %d utterances, %d mel frames, %d audio timesteps (%.2f hours)." %
          (len(metadata), mel_frames, timesteps, hours))
    print("Max input length (text chars): %d" % max(len(m[5]) for m in metadata))
    print("Max mel frames length: %d" % max(int(m[4]) for m in metadata))
    print("Max audio timesteps length: %d" % max(int(m[3]) for m in metadata))



def preprocess_speaker_data_aishell(speaker_dir, out_dir: Path, skip_existing: bool, hparams, dict_info):
    metadata = []
    if platform.system() == "Windows":
        split = "\\"
    else:
        split = "/" 
    # for book_dir in speaker_dir.glob("*"):
        # Gather the utterance audios and texts

    for wav_fpath in speaker_dir.glob("*.wav"):
        # D:\dataset\data_aishell\wav\train\S0002\BAC009S0002W0122.wav
            
        # Process each sub-utterance
        
        name = str(wav_fpath).split(split)[-1]
        key = name.split(".")[0]
        words = dict_info.get(key)
        if not words:
            continue
        sub_basename = "%s_%02d" % (name, 0)
        wav, text = split_on_silences_data_aishell(wav_fpath, words, hparams)
        metadata.append(process_utterance(wav, text, out_dir, sub_basename, 
                                              skip_existing, hparams))
    
    return [m for m in metadata if m is not None]

  
def split_on_silences_data_aishell(wav_fpath, words, hparams):
    # Load the audio waveform
    wav, _ = librosa.load(wav_fpath, hparams.sample_rate)
    wav = librosa.effects.trim(wav, top_db= 40, frame_length=2048, hop_length=512)[0]
    if hparams.rescale:
        wav = wav / np.abs(wav).max() * hparams.rescaling_max
    
    resp = pinyin(words, style=Style.TONE3)
    res = [v[0] for v in resp if v[0].strip()]
    res = " ".join(res)
    return wav, res

# aidatatang_200zh
def preprocess_aidatatang_200zh(datasets_root: Path, out_dir: Path, n_processes: int, 
                           skip_existing: bool, hparams,dataset=None):
    # Gather the input directories
    dataset_root = datasets_root.joinpath("aidatatang_200zh")
    # input_dirs = [dataset_root.joinpath("train-clean-100"), 
    #               dataset_root.joinpath("train-clean-360")]

    dict_info = {}
    transcript_dirs = dataset_root.joinpath("transcript/aidatatang_200_zh_transcript.txt")
    with open(transcript_dirs,"rb") as fp:
        dict_transcript = [v.decode() for v in fp]

    for v in dict_transcript:
        if not v:
            continue
        v = v.strip().replace("\n","").split(" ")
        dict_info[v[0]] = " ".join(v[1:])

    input_dirs = [dataset_root.joinpath("corpus/train")]
    print("\n    ".join(map(str, ["Using data from:"] + input_dirs)))
    assert all(input_dir.exists() for input_dir in input_dirs)
    
    # Create the output directories for each output file type
    out_dir.joinpath("mels").mkdir(exist_ok=True)
    out_dir.joinpath("audio").mkdir(exist_ok=True)
    
    # Create a metadata file
    metadata_fpath = out_dir.joinpath("train.txt")
    metadata_file = metadata_fpath.open("a" if skip_existing else "w", encoding="utf-8")

    # Preprocess the dataset
    speaker_dirs = list(chain.from_iterable(input_dir.glob("*") for input_dir in input_dirs))
    func = partial(preprocess_speaker_aidatatang_200zh, out_dir=out_dir, skip_existing=skip_existing, 
                   hparams=hparams, dict_info=dict_info)
    job = Pool(n_processes).imap(func, speaker_dirs)
    for speaker_metadata in tqdm(job, "aidatatang_200zh", len(speaker_dirs), unit="speakers"):
        for metadatum in speaker_metadata:
            metadata_file.write("|".join(str(x) for x in metadatum) + "\n")
    metadata_file.close()

    # Verify the contents of the metadata file
    with metadata_fpath.open("r", encoding="utf-8") as metadata_file:
        metadata = [line.split("|") for line in metadata_file]
    mel_frames = sum([int(m[4]) for m in metadata])
    timesteps = sum([int(m[3]) for m in metadata])
    sample_rate = hparams.sample_rate
    hours = (timesteps / sample_rate) / 3600
    print("The dataset consists of %d utterances, %d mel frames, %d audio timesteps (%.2f hours)." %
          (len(metadata), mel_frames, timesteps, hours))
    print("Max input length (text chars): %d" % max(len(m[5]) for m in metadata))
    print("Max mel frames length: %d" % max(int(m[4]) for m in metadata))
    print("Max audio timesteps length: %d" % max(int(m[3]) for m in metadata))



def preprocess_speaker_aidatatang_200zh(speaker_dir, out_dir: Path, skip_existing: bool, hparams, dict_info):
    metadata = []
    if platform.system() == "Windows":
        split = "\\"
    else:
        split = "/" 
    # for book_dir in speaker_dir.glob("*"):
        # Gather the utterance audios and texts

    for wav_fpath in speaker_dir.glob("*.wav"):
        # D:\dataset\data_aishell\wav\train\S0002\BAC009S0002W0122.wav
            
        # Process each sub-utterance
        
        name = str(wav_fpath).split(split)[-1]
        key = name.split(".")[0]
        words = dict_info.get(key)
        if not words:
            continue
        sub_basename = "%s_%02d" % (name, 0)
        wav, text = split_on_silences_aidatatang_200zh(wav_fpath, words, hparams)
        metadata.append(process_utterance(wav, text, out_dir, sub_basename, 
                                              skip_existing, hparams))
    
    return [m for m in metadata if m is not None]

  
def split_on_silences_aidatatang_200zh(wav_fpath, words, hparams):
    # Load the audio waveform
    wav, _ = librosa.load(wav_fpath, hparams.sample_rate)
    wav = librosa.effects.trim(wav, top_db= 40, frame_length=2048, hop_length=512)[0]
    if hparams.rescale:
        wav = wav / np.abs(wav).max() * hparams.rescaling_max
    
    resp = pinyin(words, style=Style.TONE3)
    res = [v[0] for v in resp if v[0].strip()]
    res = " ".join(res)
    return wav, res