python run.py \
    applications.0.scheduler=fixed_pool \
    cluster=our-h100 \
    start_state=splitwise_fixedpool_homo \
    performance_model=llmcompass \
    trace.filename=rr_conv_50_2min \
    seed=0


