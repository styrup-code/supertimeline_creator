FROM mcr.microsoft.com/dotnet/sdk:10.0 AS mfte-build
ARG MFTECMD_REV=b1f668d870153f9a4c4073845265c0e6071ed09f
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/EricZimmerman/MFTECmd.git /src && cd /src && git checkout "$MFTECMD_REV" && \
    dotnet publish MFTECmd/MFTECmd.csproj -c Release -f net10.0 -r linux-x64 --self-contained true -p:TargetFrameworks=net10.0 -o /publish

FROM log2timeline/plaso:latest
USER root
RUN apt-get update && apt-get install -y --no-install-recommends --no-remove 7zip libdate-manip-perl libicu78 && \
    cd /tmp && apt-get download sleuthkit && dpkg-deb -x sleuthkit_*.deb /tmp/sleuthkit && \
    install -m 0755 /tmp/sleuthkit/usr/bin/mactime /usr/local/bin/mactime && \
    rm -rf /tmp/sleuthkit /tmp/sleuthkit_*.deb /var/lib/apt/lists/* && \
    log2timeline.py --version && mactime -V
COPY --from=mfte-build /publish /opt/mftecmd/
COPY patch_plaso.py /opt/supertimeline/patch_plaso.py
RUN python3 /opt/supertimeline/patch_plaso.py
COPY runner.py /opt/supertimeline/runner.py
COPY vhdx_reader.py /opt/supertimeline/vhdx_reader.py
COPY disk_reader.py /opt/supertimeline/disk_reader.py
COPY timeline_noise.txt /opt/supertimeline/timeline_noise.txt
COPY event_logs.txt event_ids.txt /opt/supertimeline/
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["python3", "/opt/supertimeline/runner.py"]
