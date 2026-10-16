# Lecture to Notes (v1)

Teachers upload lecture recordings (video or audio). The server extracts the audio with **FFmpeg** and transcribes
it locally with **Whisper** ([faster-whisper](https://github.com/SYSTRAN/faster-whisper)). The timestamped transcript
is stored in the database as JSON. Students can browse lectures, play them, and click any line of the transcript to
jump to that moment.

