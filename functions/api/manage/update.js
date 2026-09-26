export async function onRequest(context) {
    const { request, env } = context;
    const base = String(env.UPDATE_AGENT_URL || '').replace(/\/$/, '');

    if (!base) {
        return Response.json({
            ok: false,
            error: 'Docker updater is not configured on this deployment'
        }, { status: 503 });
    }

    try {
        if (request.method === 'GET') {
            const response = await fetch(base + '/status', {
                headers: { 'Accept': 'application/json' },
                signal: AbortSignal.timeout(150000),
            });
            const body = await response.text();
            return new Response(body, {
                status: response.status,
                headers: {
                    'Content-Type': 'application/json; charset=utf-8',
                    'Cache-Control': 'no-store',
                },
            });
        }

        if (request.method === 'POST') {
            const response = await fetch(base + '/update', {
                method: 'POST',
                headers: { 'Accept': 'application/json' },
                signal: AbortSignal.timeout(10000),
            });
            const body = await response.text();
            return new Response(body, {
                status: response.status,
                headers: {
                    'Content-Type': 'application/json; charset=utf-8',
                    'Cache-Control': 'no-store',
                },
            });
        }

        return new Response('Method Not Allowed', { status: 405 });
    } catch (error) {
        return Response.json({
            ok: false,
            error: 'Updater unavailable: ' + (error?.message || String(error)),
        }, { status: 502 });
    }
}
