#!/bin/bash
#SBATCH --job-name=metadata-dashboard
#SBATCH --time=8:00:00
#SBATCH --partition=shared
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --mem=4G
#SBATCH --output=logs/dashboard_%j.log


APP_DIR="/scratch/project/k03/support_team/Metadata_Dashboard"
SIF="${APP_DIR}/dashboard.sif"
SCRATCH_BASE="/scratch/project/k03/support_team"
DB_DIR="/scratch/project/k03/support_team/mock_db:/scratch/project/k03/support_team/mock_db"

module load singularity
export SINGULARITY_CACHEDIR=${SCRATCH}/singularity_cache
export SINGULARITY_TMPDIR=${SCRATCH}/singularity_tmp
mkdir -p $SINGULARITY_CACHEDIR $SINGULARITY_TMPDIR

# ── get a free port the same way the code-server script does ──────────────────
port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("", 0)); print(s.getsockname()[1]); s.close()')
node=$(/bin/hostname -s)
user=$(whoami)
submit_host=${SLURM_SUBMIT_HOST}

mkdir -p "${APP_DIR}/logs"

echo ""
echo "Copy the following line into a new terminal to create a secure SSH tunnel between your computer and the Shaheen compute node."
echo ""
echo "ssh -L ${port}:${node}:${port} ${user}@${submit_host}.hpc.kaust.edu.sa"
echo ""
echo "Then open your browser at: http://localhost:${port}"
echo ""

singularity exec \
    --bind "${APP_DIR}:/app" \
    --bind "${DB_DIR}" \
    --bind "${SCRATCH_BASE}:${SCRATCH_BASE}" \
    "$SIF" \
    streamlit run /app/app.py \
        --server.port ${port} \
        --server.address 0.0.0.0 \
        --server.headless true \
        --browser.gatherUsageStats false
