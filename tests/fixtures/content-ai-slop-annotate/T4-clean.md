We moved the billing service to the new cluster on Tuesday. It went badly at first.

The connection pool was still sized for the old machines, so checkout requests queued and timed out for twenty minutes. Priya spotted it in the error logs before any customer wrote in. She halved the pool size and redeployed. The queue drained within a minute.

Nobody had tested the pool settings because the staging cluster runs on the same hardware production used last year. Staging should match what we run now, not what we ran when someone set it up.

Ben is rebuilding staging this week. Until he's done, any change that touches database connections gets a second reviewer.
