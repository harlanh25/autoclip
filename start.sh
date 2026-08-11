#!/bin/bash
# CFB Clip Studio - Start Script
cd /home/harlanhgharris/cfb_clip_studio
nohup python3 app.py > cfb_clip_studio.log 2>&1 &
echo "CFB Clip Studio started on port 5000"
echo "Access at http://$(curl -s ifconfig.me):5000"
