import os
from pydub import AudioSegment
import imageio_ffmpeg

ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
AudioSegment.converter = ffmpeg_path

def convert_m4a_to_wav(input_path: str, output_path: str = None) -> str:
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"File not found: {input_path}")

    if output_path is None:
        output_path = os.path.splitext(input_path)[0] + ".wav"

    audio = AudioSegment.from_file(input_path, format="m4a")
    audio.export(output_path, format="wav")
    
    print(f"Converted: {input_path} -> {output_path}")
    return output_path

if __name__ == "__main__":
    current_dir = os.getcwd()

    print(f"Current working directory: {current_dir}")
    filepath = os.path.join(current_dir, "backend", "audio", "Recording.m4a")
    print(f"File path: {filepath}")
    
    convert_m4a_to_wav(filepath)