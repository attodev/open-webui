<!-- src/lib/components/chat/Messages/NewapiReconnectBanner.svelte -->
<script lang="ts">
	import { getContext } from 'svelte';
	import { config } from '$lib/stores';

	const i18n = getContext('i18n');
</script>

<div
	class="flex items-center gap-2 text-sm text-yellow-700 dark:text-yellow-300 bg-yellow-50 dark:bg-yellow-900/20 rounded-lg px-3 py-2"
>
	<span>{$i18n.t('Your connection to new-api has expired.')}</span>
	<a href={$config?.newapi_sso?.entry_url} class="underline font-medium">
		{$i18n.t('Click here to reconnect')}
	</a>
</div>
