# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0


from datetime import datetime
import argparse, sys

import boto3


def download_all_logs(log_group_name, log_stream_name):
    """
    Download all log events from a CloudWatch log stream.
    
    Args:
        log_group_name: Name of the CloudWatch log group
        log_stream_name: Name of the log stream
    
    """
    client = boto3.client('logs', region_name='us-east-1')
    
    all_events = []
    next_token = None
    
    print(f"Downloading logs from {log_group_name}/{log_stream_name}...", file=sys.stderr)
    
    while True:
        # Prepare parameters
        params = {
            'logGroupName': log_group_name,
            'logStreamName': log_stream_name,
            'startFromHead': True  # Start from earliest events
        }
        
        # Add next token if we have one
        if next_token:
            params['nextToken'] = next_token
        
        # Get log events
        response = client.get_log_events(**params)
        
        events = response['events']
        all_events.extend(events)
        
        print(f"Retrieved {len(events)} events (total: {len(all_events)})", file=sys.stderr)
        
        # Check if we're done
        new_next_token = response['nextForwardToken']
        if new_next_token == next_token or len(events) == 0:
            # No more events to retrieve
            break
        
        next_token = new_next_token

    print(f"Download complete! Total events: {len(all_events)}", file=sys.stderr)

    for event in all_events:
        timestamp = datetime.fromtimestamp(event['timestamp'] / 1000)
        print(f"[{timestamp}] {event['message']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Download logs from cloudwatch, handling pagination."
    )
    parser.add_argument("--log-group-name", type=str, required=True,
                        help="Name of the CloudWatch log group."
    )
    parser.add_argument("--log-stream-name", type=str, required=True,
                    help="Name of the CloudWatch log stream."
    )

    args = parser.parse_args()
    LOG_GROUP = args.log_group_name
    LOG_STREAM = args.log_stream_name
    
    download_all_logs(LOG_GROUP, LOG_STREAM)
