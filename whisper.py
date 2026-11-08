# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import boto3
import json

# Initialize SageMaker Runtime client
sagemaker_runtime = boto3.client('sagemaker-runtime', region_name='us-east-1')

def transcribe_with_whisper(audio_chunk, endpoint_name):
    # Convert audio to hex string format
    hex_audio = audio_chunk.hex()

    # Create payload for Whisper model
    payload = {
        "audio_input": hex_audio,
        "language": "english",
        "task": "transcribe",
        "top_p": 0.9
    }

    # Invoke the SageMaker endpoint running Whisper
    response = sagemaker_runtime.invoke_endpoint(
        EndpointName=endpoint_name,
        ContentType='application/json',
        Body=json.dumps(payload)
    )

    # Parse the transcription response
    response_body = json.loads(response['Body'].read().decode('utf-8'))
    transcription_text = response_body['text']

    return transcription_text

# Example usage
# Read your audio file as bytes
with open("/home/ubuntu/Acequia Madre.mp3", "rb") as f:
    audio_data = f.read()

# Replace with your actual endpoint name from deployment
endpoint_name = 'endpoint-quick-start-3h45t'

transcription = transcribe_with_whisper(audio_data, endpoint_name)
print(transcription)
