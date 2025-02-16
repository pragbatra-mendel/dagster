import {Alert, Box} from '@dagster-io/ui';
import {BorderSetting} from '@dagster-io/ui/src/components/types';
import React from 'react';
import {Link} from 'react-router-dom';

import {LiveDataForNode} from '../asset-graph/Utils';
import {titleForRun} from '../runs/RunUtils';

export const FailedRunsSinceMaterializationBanner: React.FC<{
  liveData?: LiveDataForNode;
  border: BorderSetting;
}> = ({liveData, border}) => {
  const {runWhichFailedToMaterialize} = liveData || {};

  if (runWhichFailedToMaterialize) {
    return (
      <Box padding={{vertical: 16, left: 24, right: 12}} border={border}>
        <Alert
          intent="warning"
          title={
            <span>
              Run{' '}
              <Link to={`/instance/runs/${runWhichFailedToMaterialize.id}`}>
                {titleForRun({runId: runWhichFailedToMaterialize.id})}
              </Link>{' '}
              failed to materialize this asset.
            </span>
          }
        />
      </Box>
    );
  }
  return null;
};
